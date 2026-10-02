"""AUDIT P1.3: граница as_of по факту закрытия бара (решение владельца).

Метка ТФ-бара = НАЧАЛО бакета (канон проекта: T-Invest, app.marketdata.resampler,
docs/architecture). Значит бар с меткой T описывает интервал [T, T+TF) и НЕ
закрыт в момент T.

Поэтому видимость — по закрытию: бар виден, когда ts + TF <= as_of.
Прежний фильтр `ts <= as_of` на START-метках включал незакрытый бар, то есть на
replay-старте ровно в T подглядывал в минуты, которых рантайм ещё не видел.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.bars import bar_close_ts, bar_is_visible
from app.bot.universe.domain import InstrumentRef
from app.bot.universe.features import compute_feature_set, compute_market_features
from app.bot.universe.trend import compute_trend_features
from app.bot.universe.volatility import compute_volatility_features

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")


def _bars(n=60):
    from app.engine.models import Candle as EngineCandle

    out = []
    for i in range(n):
        base = 100 + 0.7 * i
        out.append(
            EngineCandle(
                ts=T0 + STEP * i, open=base, high=base + 0.5, low=base - 0.5,
                close=base + 0.1, volume=1000.0,
            )
        )
    return out


# ── Канон: метка = начало бакета ────────────────────────────────────────


def test_bar_label_is_bucket_start_not_close():
    """Метка указывает на НАЧАЛО интервала, а не на момент закрытия.

    Бар [T0, T0+5) закрывается и эмитится с меткой T0.
    """
    from dataclasses import dataclass

    from app.marketdata.resampler import Resampler

    @dataclass
    class _B:
        figi: str
        ts: datetime
        open: float = 1.0
        high: float = 1.0
        low: float = 1.0
        close: float = 1.0
        volume: float = 1.0

    r = Resampler("5min")
    for i in range(5):
        assert r.feed(_B("F", T0 + timedelta(minutes=i))) is None
    emitted = r.feed(_B("F", T0 + timedelta(minutes=5)))

    assert emitted is not None
    assert emitted.ts == T0
    assert bar_close_ts(emitted, STEP) == T0 + STEP


# ── Контракт видимости ──────────────────────────────────────────────────


def test_bar_not_visible_at_its_own_label():
    """На метке своего бара он ещё не закрыт — главный регресс P1.3."""
    bar = _bars(1)[0]
    assert bar_is_visible(bar, as_of=bar.ts, interval=STEP) is False


def test_bar_visible_at_its_close():
    """Граница включительная: в момент закрытия бар виден."""
    bar = _bars(1)[0]
    assert bar_is_visible(bar, as_of=bar.ts + STEP, interval=STEP) is True


def test_bar_invisible_one_microsecond_before_close():
    bar = _bars(1)[0]
    assert bar_is_visible(
        bar, as_of=bar.ts + STEP - timedelta(microseconds=1), interval=STEP
    ) is False


def test_bar_visible_after_close():
    bar = _bars(1)[0]
    assert bar_is_visible(bar, as_of=bar.ts + STEP + timedelta(hours=1), interval=STEP) is True


# ── Меры применяют строгую границу ─────────────────────────────────────


def test_unclosed_bar_excluded_from_features():
    bars = _bars(60)
    as_of = bars[-1].ts
    fs = compute_feature_set(SBER, bars, as_of=as_of)
    assert fs.valid
    assert fs.bars_used == 59
    assert fs.close == pytest.approx(bars[-2].close)


def test_bar_included_once_closed():
    bars = _bars(60)
    fs = compute_feature_set(SBER, bars, as_of=bars[-1].ts + STEP)
    assert fs.valid
    assert fs.bars_used == 60
    assert fs.close == pytest.approx(bars[-1].close)


def test_all_measures_share_the_same_boundary():
    """Volatility, trend и MarketFeatures не разъезжаются по границе."""
    bars = _bars(60)
    as_of = bars[-1].ts

    vol = compute_volatility_features(SBER, bars, as_of=as_of)
    trend = compute_trend_features(SBER, bars, as_of=as_of, window=44)
    mkt = compute_market_features(SBER, bars, as_of=as_of)

    assert vol.bars_used == trend.bars_used == 59
    assert mkt.volatility.bars_used == mkt.trend.bars_used == 59
    assert vol.valid and trend.valid and mkt.valid


def test_boundary_consistent_across_as_of_sweep():
    """Ни на одном as_of слои не видят разное число баров."""
    bars = _bars(80)
    for i in range(len(bars)):
        as_of = bars[i].ts
        vol = compute_volatility_features(SBER, bars, as_of=as_of)
        trend = compute_trend_features(SBER, bars, as_of=as_of, window=44)
        assert vol.bars_used == trend.bars_used == i, i


# ── Look-ahead: главные регрессы ────────────────────────────────────────


def test_future_bar_never_leaks_into_features():
    """Бар строго после as_of не влияет на признаки."""
    from app.engine.models import Candle as EngineCandle

    bars = _bars(60)
    as_of = bars[-1].ts
    baseline = compute_market_features(SBER, bars, as_of=as_of)

    poison = bars + [
        EngineCandle(
            ts=as_of + STEP, open=1.0, high=9999.0, low=0.01, close=8888.0, volume=10**9
        )
    ]
    got = compute_market_features(SBER, poison, as_of=as_of)

    assert got.volatility.atr == pytest.approx(baseline.volatility.atr, rel=1e-12)
    assert got.volatility.atr_pct == baseline.volatility.atr_pct
    assert got.trend.direction == baseline.trend.direction
    assert got.trend.strength == pytest.approx(baseline.trend.strength, rel=1e-12)


def test_bar_at_as_of_with_extreme_values_is_excluded():
    """Сценарий из аудита: бар ts == as_of с экстремальными значениями.

    Именно этот бар раньше заглядывал в будущее. Теперь он отброшен, поэтому
    результат совпадает с расчётом, где такого бара просто нет.
    """
    from app.engine.models import Candle as EngineCandle

    bars = _bars(60)
    as_of = bars[-1].ts
    baseline = compute_market_features(SBER, bars, as_of=as_of)

    poisoned = bars + [
        EngineCandle(
            ts=as_of, open=1.0, high=99999.0, low=0.001, close=99999.0, volume=10**12
        )
    ]
    got = compute_market_features(SBER, poisoned, as_of=as_of)

    assert got.volatility.atr == pytest.approx(baseline.volatility.atr, rel=1e-12)
    assert got.volatility.atr_pct == baseline.volatility.atr_pct
    assert got.trend.direction == baseline.trend.direction
    assert got.trend.slope == pytest.approx(baseline.trend.slope, rel=1e-12)
    assert got.volatility.bars_used == baseline.volatility.bars_used == 59


def test_replay_start_on_bucket_boundary_sees_nothing_of_that_bucket():
    """Replay стартует ровно в T — бар [T, T+5) ещё не участвует."""
    bars = _bars(60)
    as_of = bars[-1].ts
    assert compute_feature_set(SBER, bars, as_of=as_of).bars_used == 59
    assert compute_feature_set(SBER, bars, as_of=as_of + STEP).bars_used == 60


def test_no_visible_bars_is_invalid():
    fs = compute_feature_set(SBER, _bars(1), as_of=T0)
    assert fs.valid is False
    assert fs.bars_used == 0
    assert fs.atr is None