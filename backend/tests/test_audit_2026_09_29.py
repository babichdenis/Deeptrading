"""Регрессии аудита торгового движка 2026-09-29 (docs/osengine/AUDIT_2026-09-29.md, §6).

Покрывают P0/P1-исправления: ENG-001 (look-ahead плана входа), ENG-002
(двухфазные BE/trailing), ENG-003 (limit-entry отклоняется), ENG-004
(mode=short + session policy), ENG-005 (DD от стартового капитала),
ENG-006 (пустой вход), ENG-007 (повторный run детерминирован), ENG-008
(warmup-история stateful-стратегий), ENG-009 (Breakout), ENG-010 (Wilder
ADX reference-вектор), ENG-012 (OSE close = exit-intent), ENG-013 (M5 без
формирующегося бакета), ENG-015 (инварианты конфига/свечей), ENG-016/017
(метрики/цепочки позиций).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    EngineConfig,
    EngineRunner,
    FixedSlTpPolicy,
    MacdCrossStrategy,
    ScriptedStrategy,
    Signal,
    SignalPolicyConfig,
    Side,
)
from app.engine.exits import AtrStopPolicy
from app.engine.ledger import TradeLedger
from app.engine.metrics import full_report, summarize
from app.engine.models import Candle, Trade
from app.engine.ose.robots import Side as RobotSide
from app.engine.ose.strategy import _vote_from_fills
from app.engine.regime_strategies import BreakoutStrategy, RegimeResult
from app.engine.sessions import SessionPolicyConfig

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)  # понедельник, 13:00 MSK


def bar(o, h, lo, c, i=0, step=5):
    return Candle(ts=T0 + timedelta(minutes=step * i), open=o, high=h, low=lo,
                  close=c, volume=1000)


def series(rows, step=5):
    return [bar(o, h, lo, c, i, step) for i, (o, h, lo, c) in enumerate(rows)]


def flat(n, price=100.0):
    return [(price, price + 0.2, price - 0.2, price) for _ in range(n)]


def sig(idx, side, kind="entry"):
    return ScriptedStrategy({idx: Signal(strategy_id="s", side=side, time=T0,
                                          reason="test", kind=kind)})


def runner(strategy, exit_policy=None, **cfg_kwargs):
    return EngineRunner(
        strategy=strategy,
        exit_policy=exit_policy or FixedSlTpPolicy(stop_pct=0.01, target_pct=0.05),
        config=EngineConfig(figi="T", **cfg_kwargs),
    )


# ---------------------------------------------------------------- ENG-001

def test_entry_plan_cannot_see_execution_bar_ohlc():
    """ENG-001: план выходов не зависит от OHLC бара исполнения (вход по open)."""
    hist = [(100.0, 100.3, 99.7, 100.0), (100.0, 100.4, 99.6, 100.1),
            (100.1, 100.5, 99.8, 100.2)]

    def plan_stop(exec_high, exec_low, exec_close):
        rows = hist + [
            (100.0, exec_high, exec_low, exec_close),
            (exec_close, exec_close + 0.1, exec_close - 0.1, exec_close),
        ]
        led = runner(sig(2, Side.BUY),
                     exit_policy=AtrStopPolicy(period=2, multiplier=2.0)).run(series(rows))
        assert len(led.trades) == 1
        return led.trades[0].initial_stop

    assert plan_stop(100.2, 99.9, 100.1) == plan_stop(150.0, 50.0, 149.0)


# ---------------------------------------------------------------- ENG-002

def test_close_based_be_becomes_active_next_bar_only():
    """ENG-002: BE, активированный close-ом бара, действует со следующего бара."""
    rows = flat(3) + [
        (100.0, 100.2, 99.8, 100.05),   # вход LONG open=100
        (100.05, 101.5, 99.95, 101.2),  # close даёт 1R -> BE=100; low 99.95 < 100
        (101.2, 101.3, 99.9, 100.0),    # стоп 100 выбит -> выход
    ]
    led = runner(sig(2, Side.BUY), be_trigger_r=1.0).run(series(rows))
    assert len(led.trades) == 1
    t = led.trades[0]
    assert t.exit_index == 5, "BE не должен выбивать позицию на баре активации"
    assert t.exit_reason == "stop_loss"


def test_trailing_update_becomes_active_next_bar_only():
    """ENG-002: подтянутый трейлинг-стоп действует со следующего бара."""
    policy = AtrStopPolicy(period=14, multiplier=0.5,
                           trail_activation_comm_mult=1.0, trail_distance_r=1.0)
    rows = flat(3) + [
        (100.0, 100.2, 99.9, 100.1),   # вход LONG qty=10
        (100.1, 101.2, 100.1, 101.2),  # активация; стоп станет ~100.2 (со след. бара)
        (101.0, 101.1, 99.8, 100.0),   # low пробивает трейлинг -> выход
    ]
    led = runner(sig(2, Side.BUY), exit_policy=policy, qty=10).run(series(rows))
    assert len(led.trades) == 1
    t = led.trades[0]
    assert t.exit_index == 5, "трейлинг не должен выбивать позицию на баре пересчёта"
    assert t.exit_reason == "stop_loss"
    assert any("TRAILING_ACTIVATED" in a.detail for a in led.audit)


# ---------------------------------------------------------------- ENG-003

def test_entry_limit_feature_is_rejected():
    """ENG-003: entry_limit_atr>0 — явная ошибка валидации, не AttributeError."""
    with pytest.raises(ValueError, match="entry_limit_atr"):
        EngineRunner(
            strategy=sig(2, Side.BUY),
            exit_policy=FixedSlTpPolicy(),
            config=EngineConfig(figi="T",
                                signal_policy=SignalPolicyConfig(entry_limit_atr=1.0)),
        )


# ---------------------------------------------------------------- ENG-004

def test_short_mode_rejects_long_with_and_without_session_policy():
    """ENG-004: mode='short' блокирует LONG независимо от session state."""
    candles = series(flat(8))
    for sess in (None, SessionPolicyConfig(overnight=True)):
        r = runner(sig(2, Side.BUY), mode="short", session_policy=sess)
        led = r.run(candles)
        assert len(led.trades) == 0
        assert any("SKIP_ENTRY mode=short" in a.detail for a in led.audit)


# ---------------------------------------------------------------- ENG-005

def test_first_trade_loss_counts_in_drawdown():
    """ENG-005: первая убыточная сделка даёт просадку от стартового капитала."""
    t = Trade(
        trade_id="T0001", figi="T", side="LONG", qty=1, entry_index=0,
        entry_time=T0, entry_price=100.0, exit_index=1,
        exit_time=T0 + timedelta(minutes=5), exit_price=90.0, bars_held=1,
        gross_pnl=-10.0, commission=0.0, slippage=0.0, net_pnl=-10.0,
        exit_reason="stop_loss",
    )
    report = full_report([t], start_capital=100.0)
    assert report["max_drawdown_pct"] == 10.0
    assert report["realised_max_drawdown_pct"] == 10.0
    assert report["max_drawdown_abs"] == 10.0
    assert report["dd_basis"] == "realised_only"


# ---------------------------------------------------------------- ENG-006

def test_empty_run_returns_empty_ledger():
    """ENG-006: run([]) — пустой журнал, без IndexError."""
    led = runner(sig(0, Side.BUY)).run([])
    assert isinstance(led, TradeLedger)
    assert led.trades == []
    assert led.fingerprint()


# ---------------------------------------------------------------- ENG-007

def test_same_runner_same_input_same_fingerprint():
    """ENG-007: повторный run() тем же runner на тех же данных детерминирован
    даже для stateful-стратегии (MacdCross накапливает EMA)."""
    rows = []
    price = 100.0
    for i in range(120):
        if i < 40:
            step = 0.30 if i % 2 else -0.25
        elif i < 55:
            step = -0.40
        else:
            step = 0.35
        price += step
        rows.append((price - 0.1, price + 0.3, price - 0.4, price))
    r = EngineRunner(strategy=MacdCrossStrategy(),
                     exit_policy=FixedSlTpPolicy(0.02, 0.04),
                     config=EngineConfig(figi="T"))
    fp1 = r.run(series(rows)).fingerprint()
    fp2 = r.run(series(rows)).fingerprint()
    assert fp1 == fp2


# ---------------------------------------------------------------- ENG-008

class _SpyStrategy:
    strategy_id = "spy"
    version = "1.0.0"

    def __init__(self, warmup):
        self._warmup = warmup
        self.calls: list[int] = []

    def warmup_bars(self):
        return self._warmup

    def on_bar(self, candles):
        self.calls.append(len(candles))
        return None


def test_stateful_strategy_receives_warmup_history():
    """ENG-008: on_bar вызывается на каждом закрытом баре, включая warmup."""
    spy = _SpyStrategy(warmup=6)
    runner(spy).run(series(flat(10)))
    assert spy.calls == [1, 2, 3, 4, 5, 6, 7, 8, 9]


# ---------------------------------------------------------------- ENG-009

def test_breakout_strategy_emits_on_prior_channel_break():
    """ENG-009: сжатие BB + пробой канала ПРЕДЫДУЩИХ баров даёт сигнал."""
    closes = ([101.0, 99.0] * 95) + [100.0] * 19 + [101.5]
    rows = []
    prev = 100.0
    for c in closes:
        o = prev
        rows.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c))
        prev = c
    candles = series(rows)
    regime = RegimeResult("TRANSITIONING", 25.0, 0.6, 1.0, 0.5)
    s = BreakoutStrategy().on_bar(candles, regime)
    assert s is not None
    assert s.side is Side.BUY
    assert s.reason == "breakout_long"


# ---------------------------------------------------------------- ENG-010

def test_adx_wilder_reference_vector():
    """ENG-010: канонический Wilder ADX — вручную посчитанный вектор.

    period=2, бары (H, L, C):
      (10,8,9) (11,9,10) (12,10,11) (11.5,10.5,11) (12,10,11.5) (11,9,10)
    Seed на i=2: sTR=4, s+DM=2, s-DM=0 -> +DI=50, DX=100; далее
    i=3: +DI=33.33 DX=100; i=4: +DI=14.29 DX=100; i=5: +DI=5.88 -DI=23.53 DX=60.
    ADX: (100+100)/2=100 (i=3), (100+100)/2=100 (i=4), (100+60)/2=80 (i=5).
    """
    from app.engine.indicatorhub import _adx

    hl = [(10.0, 8.0), (11.0, 9.0), (12.0, 10.0), (11.5, 10.5), (12.0, 10.0), (11.0, 9.0)]
    cl = [9.0, 10.0, 11.0, 11.0, 11.5, 10.0]
    candles = [bar(c, h, lo, c, i, step=1)
               for i, ((h, lo), c) in enumerate(zip(hl, cl, strict=True))]
    out = _adx(candles, 2)
    adx, pdi, mdi = out["adx"], out["+di"], out["-di"]
    assert pdi[2] == 50.0 and mdi[2] == 0.0
    assert adx[3] == 100.0
    assert adx[4] == 100.0
    assert abs(adx[5] - 80.0) < 1e-9
    assert abs(pdi[5] - 100 * 0.25 / 4.25) < 1e-9
    assert abs(mdi[5] - 100 * 1.0 / 4.25) < 1e-9


# ---------------------------------------------------------------- ENG-012

def test_ose_close_signal_is_exit_intent():
    """ENG-012: закрытие робота — exit-intent, открытие — entry-intent."""
    assert _vote_from_fills([("open_long", 100.0), ("close_long", 101.0)],
                            1, RobotSide.BUY) == (Side.SELL, "close_long", "exit")
    assert _vote_from_fills([("close_short", 100.0), ("open_long", 100.0)],
                            1, RobotSide.SELL) == (Side.BUY, "open_long", "entry")
    assert _vote_from_fills([("open_short", 100.0)], 0, None) == (
        Side.SELL, "open_short", "entry")


def test_exit_signal_ignored_when_flat():
    """ENG-012: exit-сигнал на флэте не открывает противоположную позицию."""
    r = runner(sig(2, Side.SELL, kind="exit"))
    led = r.run(series(flat(6)))
    assert led.trades == []
    assert r.exit_coverage["exit_ignored_flat"] >= 1


# ---------------------------------------------------------------- ENG-013

def test_ensemble_m5_ignores_forming_bucket(monkeypatch):
    """ENG-013: в M5-голосование попадают только ЗАКРЫТЫЕ 5m-бакеты."""
    import app.engine.ensemble_v2 as ev

    captured: dict = {}
    real = ev.build_tf

    def spy(cs, tf, **kw):
        out = real(cs, tf, **kw)
        captured["c5"] = list(out)
        return out

    monkeypatch.setattr(ev, "build_tf", spy)
    candles = [bar(100.0, 100.2, 99.8, 100.0, i, step=1) for i in range(1060)]
    ev.EnsembleVoteStrategy({}).on_bar(candles)
    c5 = captured.get("c5")
    assert c5 and len(c5) >= 210
    last_1m_min = int((candles[-1].ts - T0).total_seconds() // 60)   # 1059
    last_5m_min = int((c5[-1].ts - T0).total_seconds() // 60)        # 1055
    assert last_5m_min == 1055
    assert last_5m_min < last_1m_min
    # следующий бакет (1060) закрыт не был — его в голосовании нет
    assert math.ceil(last_1m_min / 300) * 300 > last_5m_min
    u = int(c5[-1].ts.timestamp())
    assert u % 300 == 0


# ---------------------------------------------------------------- ENG-015

@pytest.mark.parametrize("cfg_kwargs", [
    {"qty": 0},
    {"mode": "sideways"},
    {"partial_fraction": 0.0},
    {"neutral_mode": "half"},
    {"abort_r": -0.1},
])
def test_config_invariants_rejected(cfg_kwargs):
    with pytest.raises(ValueError):
        EngineRunner(strategy=sig(2, Side.BUY), exit_policy=FixedSlTpPolicy(),
                     config=EngineConfig(figi="T", **cfg_kwargs))


def test_candle_invariants_rejected():
    good = series(flat(5))
    broken_ohlc = list(good)
    broken_ohlc[2] = bar(100.0, 99.5, 100.2, 100.0, 2)  # high < low
    with pytest.raises(ValueError):
        runner(sig(0, Side.BUY)).run(broken_ohlc)

    broken_ts = list(good)
    broken_ts[3] = Candle(ts=broken_ts[2].ts, open=100.0, high=100.2, low=99.8, close=100.0)
    with pytest.raises(ValueError):
        runner(sig(0, Side.BUY)).run(broken_ts)


# ---------------------------------------------------------------- ENG-016/017

def _trade(tid, net, chain_id, reason="target"):
    return Trade(
        trade_id=tid, figi="T", side="LONG", qty=1, entry_index=0, entry_time=T0,
        entry_price=100.0, exit_index=1, exit_time=T0 + timedelta(minutes=5),
        exit_price=100.0 + net, bars_held=1, gross_pnl=net, commission=0.0,
        slippage=0.0, net_pnl=net, exit_reason=reason, chain_id=chain_id,
    )


def test_partial_trade_and_final_share_position_chain():
    """ENG-017: partial и финал — одна позиция (chain), две сделки."""
    trades = [
        _trade("T0001", -3.0, "T-P0001", reason="partial_take"),
        _trade("T0002", -4.0, "T-P0001", reason="stop_loss"),
        _trade("T0003", 5.0, "T-P0002"),
    ]
    s = summarize(trades)
    assert s["trades"] == 3
    assert s["positions"] == 2
    assert s["partial_closes"] == 1
