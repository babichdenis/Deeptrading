"""TrendMeasure (Universe 2.0): направление/сила тренда, не решение.

Тесты §25 (trend block) и §12-§15:
  21. synthetic uptrend -> UP.
  22. synthetic downtrend -> DOWN.
  23. flat series -> low/flat trend.
  24. normalized slope (slope/ATR).
  25. canonical ATR used.
  26. insufficient data.
  27. invalid data.
  28. no look-ahead.
  29. deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.domain import InstrumentRef, TrendDirection
from app.bot.universe.trend import FLAT_THRESHOLD, compute_trend_features
from app.engine.models import Candle as EngineCandle

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
REF = InstrumentRef(ticker="TRND", figi="FIGI_TRND")


def _bars(closes, v=None, start=T0):
    """Строит бары по закрытиям; диапазон high/low = close ± v (v по умолчанию 1%)."""
    if v is None:
        v = max([0.01 * abs(c) for c in closes])
        if v == 0.0:
            v = 1.0
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) + v
        lo = min(prev, c) - v
        bars.append(EngineCandle(ts=start + STEP * i, open=prev, high=hi, low=lo, close=c, volume=1000.0))
        prev = c
    return bars


def _uptrend(n=60):
    return [100.0 + 0.2 * i for i in range(n)]


def _downtrend(n=60):
    return [100.0 - 0.2 * i for i in range(n)]


def _flat(n=60, base=100.0):
    return [base] * n


# ---------------------------------------------------------------- direction


def test_uptrend_direction_up():
    bars = _bars(_uptrend())
    got = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert got.valid
    assert got.direction is TrendDirection.UP


def test_downtrend_direction_down():
    bars = _bars(_downtrend())
    got = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert got.valid
    assert got.direction is TrendDirection.DOWN


def test_flat_series_direction_flat_low_strength():
    bars = _bars(_flat())
    got = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert got.valid
    assert got.direction is TrendDirection.FLAT
    assert got.strength == 0.0
    # флаг: FLAT-порог — явная константа, не магическая
    assert FLAT_THRESHOLD == 0.0


# ---------------------------------------------------------------- normalization


def test_normalized_slope_is_slope_over_atr():
    bars = _bars(_uptrend())
    got = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert got.valid
    from app.engine.indicatorhub import _atr as hub_atr

    atr = [v for v in hub_atr(bars[-44:], 14) if v is not None][-1]
    assert got.normalized_slope == pytest.approx(got.slope / float(atr))
    assert got.strength == pytest.approx(min(abs(got.normalized_slope), 1.0))


def test_stronger_trend_stronger_strength():
    bars_weak = _bars([100.0 + 0.01 * i for i in range(60)])
    bars_strong = _bars([100.0 + 1.0 * i for i in range(60)])
    weak = compute_trend_features(REF, bars_weak, as_of=bars_weak[-1].ts, window=44)
    strong = compute_trend_features(REF, bars_strong, as_of=bars_strong[-1].ts, window=44)
    assert weak.valid and strong.valid
    assert strong.strength > weak.strength


def test_slope_scale_independent_via_normalization():
    """Наклон больше на крупном масштабе, но normalized_slope масштабно-стабилен."""
    small = [100.0 + 0.2 * i for i in range(60)]
    large = [10000.0 + 20.0 * i for i in range(60)]
    fs = compute_trend_features(REF, _bars(small), as_of=_bars(small)[-1].ts, window=44)
    fl = compute_trend_features(REF, _bars(large), as_of=_bars(large)[-1].ts, window=44)
    assert fl.slope > fs.slope
    assert fl.normalized_slope / fs.normalized_slope == pytest.approx(1.0, rel=1e-2)


# ---------------------------------------------------------------- validity


def test_insufficient_data_invalid():
    # Бар должен быть ЗАКРЫТ на as_of (граница P1.3), иначе причина была бы
    # no_data вместо insufficient_bars — тест проверяет именно нехватку истории.
    got = compute_trend_features(REF, _bars([100.0]), as_of=T0 + STEP, window=44)
    assert not got.valid
    assert got.reason == "insufficient_bars"


def test_unclosed_bar_is_no_data_not_insufficient():
    """Единственный незакрытый бар на as_of = нет данных, а не «мало баров»."""
    got = compute_trend_features(REF, _bars([100.0]), as_of=T0, window=44)
    assert not got.valid
    assert got.reason == "no_data"


def test_no_data_invalid():
    got = compute_trend_features(REF, [], as_of=T0, window=44)
    assert not got.valid
    assert got.reason == "no_data"


def test_zero_close_invalid():
    bars = _bars([100.0, 100.0, 0.0])
    got = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert not got.valid


# ---------------------------------------------------------------- look-ahead / determinism


def test_no_look_ahead_future_bar_does_not_change_result():
    bars = _bars(_uptrend(60))
    cf = bars[30].ts
    before = compute_trend_features(REF, bars, as_of=cf, window=44)
    future = _bars(_uptrend(20), start=cf + STEP)
    after = compute_trend_features(REF, bars + future, as_of=cf, window=44)
    assert before == after


def test_deterministic_same_input_same_output():
    bars = _bars(_uptrend(60))
    a = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    b = compute_trend_features(REF, bars, as_of=bars[-1].ts, window=44)
    assert a == b