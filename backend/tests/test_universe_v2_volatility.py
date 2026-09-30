"""VolatilityMeasure (Universe 2.0): измеритель волатильности, не решение.

Требования из плана §8-§11 и §25 (volatility block):
  14. ATR parity с canonical IndicatorHub.
  15. ATR%.
  16. insufficient bars.
  17. invalid data.
  18. no look-ahead.
  19. deterministic.
  20. as_of changes result only when relevant historical bars change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.domain import InstrumentRef
from app.bot.universe.volatility import compute_volatility_features
from app.engine.indicatorhub import _atr as hub_atr
from app.engine.models import Candle as EngineCandle

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")


def _bars(n: int, base: float = 100.0, drift: float = 0.1, v: float | None = None, start: datetime = T0):
    """Простой тестовый ряд: close растёт с drift, диапазон v (по умолчанию 1%)."""
    if v is None:
        v = max(0.01 * base, 1e-9)
    bars = []
    close = base
    for i in range(n):
        ts = start + STEP * i
        hi = close + v
        lo = close - v
        bars.append(
            EngineCandle(
                ts=ts, open=close, high=hi, low=lo, close=close + drift, volume=1000.0
            )
        )
        close = close + drift
    return bars


# ---------------------------------------------------------------- canonical parity


def test_atr_parity_with_canonical_indicatorhub():
    bars = _bars(60)
    got = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    canonical = [v for v in hub_atr(bars[-44:], 14) if v is not None][-1]
    assert got.valid
    assert got.atr == pytest.approx(float(canonical))


def test_atr_pct_formula():
    bars = _bars(60)
    got = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    assert got.valid
    assert got.atr_pct == pytest.approx(round(got.atr / bars[-1].close * 100, 3))


def test_atr_parity_matches_legacy_feature_layer():
    from app.bot.universe.features import compute_feature_set

    bars = _bars(60)
    v2 = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    legacy = compute_feature_set(SBER, bars, as_of=bars[-1].ts)
    assert v2.atr == pytest.approx(legacy.atr)
    assert v2.atr_pct == pytest.approx(legacy.atr_pct)


# ---------------------------------------------------------------- validity


def test_insufficient_bars_invalid():
    got = compute_volatility_features(SBER, _bars(3), as_of=T0 + STEP * 10)
    assert not got.valid
    assert got.reason == "insufficient_bars"
    assert got.atr is None


def test_no_data_invalid():
    got = compute_volatility_features(SBER, [], as_of=T0)
    assert not got.valid
    assert got.reason == "no_data"


def test_zero_close_invalid():
    bars = _bars(60)
    bars[-1] = EngineCandle(
        ts=bars[-1].ts, open=0.0, high=1.0, low=0.0, close=0.0, volume=0.0
    )
    got = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    assert not got.valid
    assert got.reason == "invalid_close"


# ---------------------------------------------------------------- look-ahead


def test_no_look_ahead_future_bar_does_not_change_result():
    bars = _bars(60)
    cf = bars[30].ts
    before = compute_volatility_features(SBER, bars, as_of=cf)
    future = _bars(30, start=cf + STEP)
    after = compute_volatility_features(SBER, bars + future, as_of=cf)
    assert before == after


def test_as_of_moves_only_when_relevant_history_changes():
    bars = _bars(60)
    t1 = bars[20].ts
    f1 = compute_volatility_features(SBER, bars, as_of=t1)
    # as_of позже (новые бары видны) → результат может измениться
    f2 = compute_volatility_features(SBER, bars, as_of=bars[59].ts)
    assert f1 != f2
    # а as_of чуть дальше без новых баров → ATR/valid те же (as_of — часть
    # контракта и меняется по определению, признаки нет)
    t1_plus = t1 + timedelta(seconds=30)
    f1b = compute_volatility_features(SBER, bars, as_of=t1_plus)
    assert f1.atr == f1b.atr
    assert f1.atr_pct == f1b.atr_pct
    assert f1.valid == f1b.valid
    assert f1.bars_used == f1b.bars_used


# ---------------------------------------------------------------- determinism


def test_deterministic_same_input_same_output():
    bars = _bars(60)
    a = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    b = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    assert a == b


def test_realized_volatility_and_range_pct_populated():
    bars = _bars(60)
    got = compute_volatility_features(SBER, bars, as_of=bars[-1].ts)
    assert got.valid
    assert got.realized_volatility is not None and got.realized_volatility >= 0
    assert got.range_pct is not None and got.range_pct > 0