"""Юнит-тесты чистых модулей движка (без БД и сети).

Покрывает (audit 2026-09-18):
  - app/services/candle_guard.py: bar_ok / jump_ratio / bad_days_flicker / validate_candles
  - app/engine/quorum.py: merge_quorum (голосование, кворум, оппозиция, funnel)
  - app/engine/regime_strategies.py: detect_regime + фильтры стратегий по режиму + ExitManager
  - app/engine/catalog.py: инварианты каталога стратегий + canonical_params_hash
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.engine.catalog import STRATEGY_CATALOG, StrategyCard, canonical_params_hash
from app.engine.models import Candle, Side
from app.engine.quorum import merge_quorum
from app.engine.regime_strategies import (
    BreakoutStrategy,
    ExitManager,
    MeanReversionStrategy,
    RegimeFilteredStrategies,
    RegimeResult,
    TrendFollowingStrategy,
    create_strategies_by_regime,
    detect_regime,
)
from app.services.candle_guard import (
    bad_days_flicker,
    bar_ok,
    jump_ratio,
    validate_candles,
)

MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)


# ============================================================
# Хелперы
# ============================================================
def _c(i, o, h, lo, c, v=1000, ts=None):
    return Candle(ts=ts or (T0 + timedelta(minutes=i)), open=o, high=h, low=lo, close=c, volume=v)


def _flat(n=220, price=100.0, step=0.0, wig=0.5, start_min=0):
    """n спокойных свечей: постоянный шаг + маленькие тени."""
    out = []
    p = price
    for i in range(n):
        o = p
        c = p + step
        out.append(_c(start_min + i, o, max(o, c) + wig, min(o, c) - wig, c))
        p = c
    return out


def _trending(n=220, price=100.0, step=0.5):
    """Строго монотонный рост: ADX→100, ER→1."""
    return _flat(n=n, price=price, step=step, wig=0.05)


def _oscillating(n=220, lo=99.0, hi=101.0):
    """Пила в канале: ER≈0, ADX≈0 → CHOPPY."""
    out = []
    for i in range(n):
        base = hi if i % 2 == 0 else lo
        o = base
        c = hi if i % 2 == 1 else lo
        out.append(_c(i, o, hi + 0.1, lo - 0.1, c))
    return out


def _high_vol(n_calm=120, n_wild=20, calm_tr=0.05, wild_tr=8.0):
    """Спокойный хвост + взрыв волатильности: atr_ratio > 1.8 → HIGH_VOLATILITY."""
    out = []
    p = 100.0
    for i in range(n_calm):
        o = p
        c = p + calm_tr
        out.append(_c(i, o, max(o, c) + calm_tr, min(o, c) - calm_tr, c))
        p = c
    for i in range(n_wild):
        o = p
        c = p + wild_tr
        out.append(_c(n_calm + i, o, max(o, c) + wild_tr, min(o, c) - wild_tr, c))
        p = c
    return out


# ============================================================
# candle_guard
# ============================================================
class TestBarOk:
    def test_valid_bar(self):
        assert bar_ok(100, 101, 99, 100.5) is True

    def test_valid_bar_with_volume(self):
        assert bar_ok(100, 101, 99, 100.5, 500) is True

    def test_negative_volume_rejected(self):
        assert bar_ok(100, 101, 99, 100.5, -1) is False

    def test_zero_price_rejected(self):
        assert bar_ok(0, 101, 99, 100.5) is False
        assert bar_ok(100, 101, 99, 0) is False

    def test_negative_price_rejected(self):
        assert bar_ok(100, 101, -1, 100.5) is False

    def test_high_below_low_rejected(self):
        assert bar_ok(100, 99, 101, 100) is False

    def test_ohlc_not_contained_rejected(self):
        # close выше high
        assert bar_ok(100, 101, 99, 102) is False
        # open ниже low
        assert bar_ok(98, 101, 99, 100) is False

    def test_garbage_input_rejected(self):
        assert bar_ok(None, 101, 99, 100) is False
        assert bar_ok("x", "y", "z", "w") is False

    def test_none_volume_ignored(self):
        assert bar_ok(100, 101, 99, 100.5, None) is True


class TestJumpRatio:
    def test_normal_jump(self):
        assert jump_ratio(100.0, 110.0) == pytest.approx(0.1)

    def test_negative_jump_is_absolute(self):
        assert jump_ratio(100.0, 90.0) == pytest.approx(0.1)

    def test_no_prev(self):
        assert jump_ratio(None, 100.0) is None

    def test_zero_prev(self):
        assert jump_ratio(0.0, 100.0) is None

    def test_garbage(self):
        assert jump_ratio("x", 100.0) is None


class TestBadDaysFlicker:
    def _day_candles(self, day_start_min, closes):
        out = []
        base = T0 + timedelta(minutes=day_start_min)
        for k, c in enumerate(closes):
            ts = base + timedelta(minutes=k)
            out.append(Candle(ts=ts, open=c, high=c * 1.001, low=c * 0.999, close=c, volume=10))
        return out

    def test_flickering_day_flagged(self):
        # 4 прыжка > 50% внутри одного МСК-дня
        closes = [100, 200, 80, 190, 70]
        candles = self._day_candles(0, closes)
        bad = bad_days_flicker(candles, MSK)
        assert len(bad) == 1
        d = T0.astimezone(MSK).date().isoformat()
        assert d in bad

    def test_calm_day_not_flagged(self):
        closes = [100, 100.5, 101, 100.7, 101.2]
        candles = self._day_candles(0, closes)
        assert bad_days_flicker(candles, MSK) == set()

    def test_two_jumps_below_threshold_not_flagged(self):
        # ровно 2 прыжка (< flicker_jumps=3) — день не бракуем
        closes = [100, 200, 80, 79.9]
        candles = self._day_candles(0, closes)
        assert bad_days_flicker(candles, MSK) == set()


class TestValidateCandles:
    def test_good_series_passes(self):
        candles = _flat(n=50, step=0.1)
        valid, skipped = validate_candles(candles, MSK)
        assert skipped == 0
        assert len(valid) == 50

    def test_broken_ohlc_skipped(self):
        candles = _flat(n=10, step=0.1)
        candles[5] = _c(5, 100, 99, 101, 100)  # high < low
        valid, skipped = validate_candles(candles, MSK)
        assert skipped == 1
        assert len(valid) == 9
        assert candles[5] not in valid

    def test_big_jump_skipped(self):
        candles = _flat(n=10, step=0.1, price=100.0)
        candles[5] = _c(5, 200, 201, 199, 200)  # прыжок +100% от prev_close
        valid, skipped = validate_candles(candles, MSK)
        assert skipped >= 1

    def test_flicker_day_dropped_entirely(self):
        # день с мерцанием: все его свечи отбрасываются
        day1 = [
            Candle(ts=T0 + timedelta(minutes=k), open=c, high=c * 1.001, low=c * 0.999,
                  close=c, volume=10)
            for k, c in enumerate([100, 200, 80, 190, 70])
        ]
        day2_base = T0 + timedelta(days=1)
        day2 = [
            Candle(ts=day2_base + timedelta(minutes=k), open=c, high=c * 1.001, low=c * 0.999,
                  close=c, volume=10)
            for k, c in enumerate([100, 100.5, 101, 101.5, 102])
        ]
        valid, skipped = validate_candles(day1 + day2, MSK)
        assert skipped == len(day1)
        assert len(valid) == len(day2)

    def test_empty_input(self):
        valid, skipped = validate_candles([], MSK)
        assert valid == []
        assert skipped == 0


# ============================================================
# quorum: merge_quorum
# ============================================================
def _sig(ts, side):
    return {"ts": ts, "side": side}


class TestMergeQuorum:
    def test_two_votes_same_ts_same_side(self):
        runs = [
            ("s1", [_sig(T0, "BUY")]),
            ("s2", [_sig(T0, "BUY")]),
            ("s3", []),
        ]
        merged, funnel = merge_quorum(runs, 2)
        assert len(merged) == 1
        m = merged[0]
        assert m["side"] == "BUY"
        assert m["features"]["votes"] == 2
        assert m["features"]["members_for"] == ["s1", "s2"]
        assert m["features"]["opposition"] == []
        assert m["reason"] == "quorum_2of3"
        assert funnel["quorum"]["signals"] == 1
        assert funnel["quorum"]["BUY"] == 1

    def test_below_quorum_rejected(self):
        runs = [
            ("s1", [_sig(T0, "BUY")]),
            ("s2", []),
            ("s3", []),
        ]
        merged, _ = merge_quorum(runs, 2)
        assert merged == []

    def test_tie_rejected_even_above_quorum(self):
        runs = [
            ("s1", [_sig(T0, "BUY")]),
            ("s2", [_sig(T0, "BUY")]),
            ("s3", [_sig(T0, "SELL")]),
            ("s4", [_sig(T0, "SELL")]),
        ]
        merged, _ = merge_quorum(runs, 2)
        assert merged == []

    def test_strong_minority_loses_to_majority(self):
        runs = [
            ("s1", [_sig(T0, "SELL")]),
            ("s2", [_sig(T0, "SELL")]),
            ("s3", [_sig(T0, "BUY")]),
        ]
        merged, _ = merge_quorum(runs, 2)
        assert len(merged) == 1
        assert merged[0]["side"] == "SELL"
        assert merged[0]["features"]["opposition"] == ["s3"]

    def test_events_sorted_by_ts(self):
        # оба члена голосуют на обеих метках (кворум на каждой);
        # вход s1 идёт в обратном порядке — выход должен быть отсортирован по ts
        t1, t2 = T0, T0 + timedelta(minutes=30)
        runs = [
            ("s1", [_sig(t2, "BUY"), _sig(t1, "BUY")]),
            ("s2", [_sig(t1, "BUY"), _sig(t2, "BUY")]),
        ]
        merged, _ = merge_quorum(runs, 2)
        assert [m["ts"] for m in merged] == [t1, t2]

    def test_quorum_one(self):
        runs = [("s1", [_sig(T0, "BUY")])]
        merged, _ = merge_quorum(runs, 1)
        assert len(merged) == 1
        assert merged[0]["features"]["votes"] == 1

    def test_empty_runs(self):
        merged, funnel = merge_quorum([], 2)
        assert merged == []
        assert funnel["quorum"]["signals"] == 0

    def test_funnel_raw_counts(self):
        runs = [
            ("s1", [_sig(T0, "BUY"), _sig(T0 + timedelta(minutes=1), "SELL")]),
        ]
        _, funnel = merge_quorum(runs, 1)
        assert funnel["s1:raw"] == {"BUY": 1, "SELL": 1}


# ============================================================
# regime_strategies: detect_regime
# ============================================================
class TestDetectRegime:
    def test_short_history_is_transitioning(self):
        r = detect_regime(_flat(n=5, step=0.1))
        assert r.regime == "TRANSITIONING"
        assert r.adx is None
        assert r.risk_multiplier == 0.5

    def test_empty_candles_no_crash(self):
        r = detect_regime([])
        assert r.regime == "TRANSITIONING"
        assert r.efficiency_ratio == 0.0

    def test_monotonic_uptrend_is_trending(self):
        r = detect_regime(_trending(n=220, step=0.5))
        assert r.regime == "TRENDING"
        assert r.risk_multiplier == 1.0
        assert r.efficiency_ratio > 0.7
        assert r.adx is not None and r.adx > 25

    def test_flat_channel_is_choppy(self):
        r = detect_regime(_oscillating(n=220))
        assert r.regime == "CHOPPY"
        assert r.risk_multiplier == 0.7
        assert r.efficiency_ratio < 0.4

    def test_volatility_spike_is_high_volatility(self):
        r = detect_regime(_high_vol())
        assert r.regime == "HIGH_VOLATILITY"
        assert r.risk_multiplier == 0.0
        assert r.atr_ratio > 1.8

    def test_regime_result_fields(self):
        r = detect_regime(_flat(n=80, step=0.2))
        assert set(vars(r)) >= {"regime", "adx", "efficiency_ratio", "atr_ratio", "risk_multiplier"}


class TestRegimeFilters:
    def _regime(self, name, risk=0.5):
        return RegimeResult(regime=name, adx=25.0, efficiency_ratio=0.5,
                            atr_ratio=1.0, risk_multiplier=risk)

    def test_trend_following_blocked_in_high_vol(self):
        s = TrendFollowingStrategy()
        assert s.on_bar(_trending(n=220), self._regime("HIGH_VOLATILITY", 0.0)) is None

    def test_trend_following_blocked_in_weak_transition(self):
        s = TrendFollowingStrategy()
        assert s.on_bar(_trending(n=220), self._regime("TRANSITIONING", 0.5)) is None

    def test_trend_following_fires_in_trending(self):
        s = TrendFollowingStrategy()
        sig = s.on_bar(_trending(n=220, step=0.5), self._regime("TRENDING", 1.0))
        assert sig is not None
        assert sig.side == Side.BUY
        assert sig.reason == "trend_long"

    def test_mean_reversion_only_in_choppy(self):
        s = MeanReversionStrategy()
        assert s.on_bar(_oscillating(n=220), self._regime("TRENDING", 1.0)) is None

    def test_breakout_not_in_choppy(self):
        s = BreakoutStrategy()
        assert s.on_bar(_oscillating(n=220), self._regime("CHOPPY", 0.7)) is None

    def test_warmup_blocks_all(self):
        for s in (TrendFollowingStrategy(), MeanReversionStrategy(), BreakoutStrategy()):
            assert s.on_bar(_flat(n=100, step=0.1), self._regime("TRENDING")) is None

    def test_create_strategies_by_regime(self):
        # сравниваем типы: у стратегий нет __eq__, == по экземплярам всегда False
        assert [type(s) for s in create_strategies_by_regime("TRENDING")] == [
            TrendFollowingStrategy, BreakoutStrategy]
        assert [type(s) for s in create_strategies_by_regime("TRANSITIONING")] == [
            TrendFollowingStrategy, BreakoutStrategy]
        assert [type(s) for s in create_strategies_by_regime("CHOPPY")] == [
            MeanReversionStrategy]
        assert create_strategies_by_regime("HIGH_VOLATILITY") == []

    def test_regime_filtered_pipeline_warmup(self):
        rfs = RegimeFilteredStrategies()
        assert rfs.on_bar(_flat(n=100, step=0.1)) == []

    def test_regime_filtered_pipeline_no_crash_on_trending(self):
        rfs = RegimeFilteredStrategies()
        out = rfs.on_bar(_trending(n=220, step=0.5))
        assert isinstance(out, list)


class TestExitManager:
    def _signal(self, side=Side.BUY, reason="trend_long"):
        from app.engine.models import Signal
        return Signal(strategy_id="t", side=side, time=T0, reason=reason)

    def test_create_position_long_levels(self):
        em = ExitManager({"default_atr_mult_stop": 2.0, "default_risk_reward": 2.0})
        pos = em.create_position(self._signal(Side.BUY), entry_price=100.0, entry_bar=0, atr=1.0)
        assert pos.stop_loss == pytest.approx(98.0)
        assert pos.take_profit == pytest.approx(104.0)

    def test_create_position_short_levels(self):
        em = ExitManager({"default_atr_mult_stop": 2.0, "default_risk_reward": 2.0})
        pos = em.create_position(self._signal(Side.SELL), entry_price=100.0, entry_bar=0, atr=1.0)
        assert pos.stop_loss == pytest.approx(102.0)
        assert pos.take_profit == pytest.approx(96.0)

    def test_stop_loss_exit_long(self):
        em = ExitManager({"enable_trailing": False, "enable_regime_exit": False,
                          "max_bars_in_trade": 100})
        pos = em.create_position(self._signal(Side.BUY), 100.0, 0, atr=1.0)
        reg = RegimeResult("TRENDING", 30.0, 0.8, 1.0, 1.0)
        candles = _flat(n=220, step=0.1)
        d = em.check_exit(pos, _c(0, 99, 99.5, 97.5, 98.2), 1, reg, candles)
        assert d.exit is True
        assert d.reason.value == "stop_loss"

    def test_take_profit_exit_long(self):
        em = ExitManager({"enable_trailing": False, "enable_regime_exit": False,
                          "max_bars_in_trade": 100})
        pos = em.create_position(self._signal(Side.BUY), 100.0, 0, atr=1.0)
        reg = RegimeResult("TRENDING", 30.0, 0.8, 1.0, 1.0)
        candles = _flat(n=220, step=0.1)
        d = em.check_exit(pos, _c(0, 103, 105, 102.9, 104.5), 1, reg, candles)
        assert d.exit is True
        assert d.reason.value == "take_profit"

    def test_time_exit(self):
        em = ExitManager({"enable_trailing": False, "enable_regime_exit": False,
                          "max_bars_in_trade": 3})
        pos = em.create_position(self._signal(Side.BUY), 100.0, 0, atr=1.0)
        reg = RegimeResult("TRENDING", 30.0, 0.8, 1.0, 1.0)
        candles = _flat(n=220, step=0.1)
        d = None
        for i in range(3):
            d = em.check_exit(pos, _c(i, 100, 100.5, 99.5, 100.1), i, reg, candles)
        assert d.exit is True
        assert d.reason.value == "time_exit"

    def test_regime_change_exit_from_trending_to_choppy(self):
        em = ExitManager({"enable_trailing": False, "max_bars_in_trade": 100})
        pos = em.create_position(self._signal(Side.BUY), 100.0, 0, atr=1.0,
                                  regime="TRENDING")
        choppy = RegimeResult("CHOPPY", 10.0, 0.2, 1.0, 0.7)
        candles = _flat(n=220, step=0.1)
        d = em.check_exit(pos, _c(0, 100, 100.5, 99.5, 100.1), 1, choppy, candles)
        assert d.exit is True
        assert d.reason.value == "regime_change"

    def test_no_exit_normal_bar(self):
        em = ExitManager({"enable_trailing": False, "enable_regime_exit": False,
                          "max_bars_in_trade": 100})
        pos = em.create_position(self._signal(Side.BUY), 100.0, 0, atr=1.0)
        reg = RegimeResult("TRENDING", 30.0, 0.8, 1.0, 1.0)
        candles = _flat(n=220, step=0.1)
        d = em.check_exit(pos, _c(0, 100, 100.4, 99.6, 100.1), 1, reg, candles)
        assert d.exit is False
        assert d.reason is None


# ============================================================
# catalog
# ============================================================
class TestCatalog:
    def test_ids_unique_and_match_keys(self):
        ids = [c.id for c in STRATEGY_CATALOG.values()]
        assert len(ids) == len(set(ids))
        for k, card in STRATEGY_CATALOG.items():
            assert card.id == k

    def test_known_core_ids_present(self):
        for sid in ("rsi_reversal", "stochastic", "bollinger_reclaim", "pullback_ema",
                    "macd_cross", "donchian_breakout", "volume_drop", "volume_climax",
                    "trend_up", "trend_down", "ensemble_vote"):
            assert sid in STRATEGY_CATALOG, sid

    def test_cards_are_strategy_card(self):
        for card in STRATEGY_CATALOG.values():
            assert isinstance(card, StrategyCard)

    def test_param_defaults_within_bounds(self):
        for card in STRATEGY_CATALOG.values():
            for pname, spec in card.params_schema.items():
                assert "type" in spec and "default" in spec, (card.id, pname)
                d = spec["default"]
                if spec.get("min") is not None:
                    assert d >= spec["min"], (card.id, pname, d, spec["min"])
                if spec.get("max") is not None:
                    assert d <= spec["max"], (card.id, pname, d, spec["max"])

    def test_waves_and_timeframes(self):
        for card in STRATEGY_CATALOG.values():
            assert card.wave in (1, 3, 4, 5, 6), card.id
            assert isinstance(card.timeframes, tuple) and card.timeframes, card.id
            for tf in card.timeframes:
                assert tf in ("1min", "5min", "10min", "15min", "hour", "day"), (card.id, tf)

    def test_status_values(self):
        for card in STRATEGY_CATALOG.values():
            assert card.status in ("DRAFT", "AVAILABLE"), card.id

    def test_to_dict_timeframes_list(self):
        for card in STRATEGY_CATALOG.values():
            d = card.to_dict()
            assert isinstance(d["timeframes"], list)


class TestCanonicalParamsHash:
    def test_stable(self):
        assert canonical_params_hash({"a": 1, "b": 2}) == canonical_params_hash({"a": 1, "b": 2})

    def test_key_order_insensitive(self):
        assert canonical_params_hash({"a": 1, "b": 2}) == canonical_params_hash({"b": 2, "a": 1})

    def test_different_params_differ(self):
        assert canonical_params_hash({"a": 1}) != canonical_params_hash({"a": 2})

    def test_empty_and_none(self):
        assert canonical_params_hash({}) == canonical_params_hash(None)
        assert isinstance(canonical_params_hash({}), str)
        assert len(canonical_params_hash({})) == 16
