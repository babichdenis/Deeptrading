"""Тесты измерений Regime v2 (C.1): каузальность, нормализация, канонические индикаторы."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.engine.models import Candle
from app.services.regime_v2.measurements import (
    RegimeV2Params,
    compute_measurements,
    directional_consistency,
    realized_vol_pct,
    rolling_percentile,
)

T0 = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)


def _bars(closes: list[float], minutes: int = 5) -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        out.append(Candle(ts=T0 + timedelta(minutes=minutes * i),
                          open=c, high=c + 0.5, low=c - 0.5, close=c, volume=1000.0))
    return out


def test_uptrend_measurements():
    bars = _bars([100.0 + i for i in range(80)])
    rows = compute_measurements(bars, RegimeV2Params(window=12))
    last = rows[-1]
    assert abs(last["er"] - 1.0) < 1e-9
    assert last["consistency"] == 1.0
    assert last["drift_atr"] > 0
    assert last["slope_atr"] > 0
    assert last["dist_ema_atr"] > 0
    assert last["di_spread"] > 0
    assert last["rv_atr"] is not None and last["rv_atr"] >= 0
    assert last["range_atr"] is not None and last["range_atr"] > 0


def test_flat_series_er_zero():
    bars = _bars([100.0] * 80)
    rows = compute_measurements(bars, RegimeV2Params(window=12))
    last = rows[-1]
    assert last["er"] == 0.0
    assert last["drift_atr"] == 0.0
    assert last["adx"] is not None


def test_warmup_none_fields():
    bars = _bars([100.0 + i for i in range(80)])
    rows = compute_measurements(bars, RegimeV2Params(window=12))
    assert rows[0]["drift_atr"] is None
    assert rows[12]["drift_atr"] is None
    assert rows[13]["drift_atr"] is not None
    assert rows[0]["atr_pct"] is None


def test_no_lookahead_prefix_parity():
    bars = _bars([100.0 + i + (i % 7) for i in range(120)])
    full = compute_measurements(bars, RegimeV2Params(window=12))
    prefix = compute_measurements(bars[:90], RegimeV2Params(window=12))
    keys = ("atr_pct", "atr_percentile", "drift_atr", "slope_atr", "dist_ema_atr",
            "er", "consistency", "di_spread", "adx", "rv_atr", "range_atr")
    for k in keys:
        assert prefix[89][k] == full[89][k], k


def test_rolling_percentile_helpers():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, None]
    out = rolling_percentile(vals, 200)
    assert out[4] == 80.0
    assert out[5] is None
    assert out[0] == 50.0
    flat = directional_consistency(_bars([100.0] * 10), 5)
    assert flat[5] == 1.0
    rv = realized_vol_pct(_bars([100.0 + i for i in range(20)]), 5)
    assert rv[5] is not None
    assert rv[0] is None
