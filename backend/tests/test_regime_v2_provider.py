"""Тесты Regime v2 C.6: RegimeProvider (legacy default, v2 analytics-only)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.engine.models import Candle
from app.services.regime import RegimeDetector
from app.services.regime_v2.hysteresis import HysteresisParams
from app.services.regime_v2.provider import RegimeProvider

T0 = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)


def _bars(closes: list[float]) -> list[Candle]:
    return [Candle(ts=T0 + timedelta(minutes=5 * i), open=c, high=c + 0.5,
                   low=c - 0.5, close=c, volume=1000.0)
            for i, c in enumerate(closes)]


def test_provider_default_is_legacy():
    p = RegimeProvider()
    assert p.provider == "legacy"
    assert p.hp == HysteresisParams()


def test_provider_unknown_raises():
    with pytest.raises(ValueError):
        RegimeProvider("v3")


def test_legacy_provider_matches_detector():
    bars = _bars([100.0 + i for i in range(120)])
    rows = RegimeProvider("legacy").run(bars, 5 * 60)
    want = RegimeDetector().compute(bars)
    assert len(rows) == len(want)
    assert [r["label"] for r in rows] == [r["state"] for r in want]


def test_v2_provider_runs_mild_and_returns_observations():
    bars = _bars([100.0 + (i % 11) for i in range(160)])
    rows = RegimeProvider("v2").run(bars, 3600)
    assert len(rows) == len(bars)
    assert all(r["label"] in ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL")
               for r in rows)
    assert "observation" in rows[-1]
    assert rows[-1]["observation"]["timeframe"] == 3600
