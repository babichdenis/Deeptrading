"""EfficiencyRatio (Кауфман) — reference-тест канон-индикатора хаба."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.engine.indicatorhub import INDICATORS, _efficiency_ratio

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _line(closes):
    return [Candle(ts=T0 + timedelta(minutes=i), open=c, high=c, low=c, close=c, volume=1.0)
            for i, c in enumerate(closes)]


def test_er_straight_line_is_one():
    bars = _line([100.0 + i for i in range(30)])
    er = _efficiency_ratio(bars, 10)
    assert er[9] is None
    assert abs(er[10] - 1.0) < 1e-12
    assert abs(er[-1] - 1.0) < 1e-12


def test_er_flat_is_zero():
    bars = _line([100.0] * 30)
    er = _efficiency_ratio(bars, 10)
    assert er[10] == 0.0
    assert er[-1] == 0.0


def test_er_sawtooth_below_one():
    closes = [100.0 + (1.0 if i % 2 else 0.0) for i in range(40)]
    er = _efficiency_ratio(_line(closes), 10)
    vals = [v for v in er if v is not None]
    assert vals
    assert all(0.0 <= v <= 1.0 for v in vals)
    assert vals[-1] < 1.0


def test_er_in_hub_registry():
    d = INDICATORS["efficiency_ratio"]
    assert d.required_bars() == 11
    assert d.category == "trend"
