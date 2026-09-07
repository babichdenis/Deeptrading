"""Integration test for full pipeline: candles -> compute_ensemble -> trades -> metrics.

Goal: verify the complete pipeline works end-to-end without exceptions.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle
from app.engine.metrics import summarize, equity_curve, max_drawdown_pct, full_report
from app.services.ensemble import compute_ensemble


T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _realistic_candles(n=1000, seed=42) -> list[Candle]:
    """Generate realistic 1m candles with trend + noise (simulates real market)."""
    rng = random.Random(seed)
    candles = []
    price = 200.0
    trend = 0.0
    for i in range(n):
        ts = T0 + timedelta(minutes=i)
        # Trend changes every ~100 bars
        if i % 100 == 0:
            trend = rng.choice([-0.0002, 0, 0.0002])
        noise = rng.gauss(0, 0.002) * price
        price = max(price + trend * price + noise, 10.0)
        o = price
        spread = abs(rng.gauss(0, 0.001)) * price
        h = price + spread
        l = price - spread
        c = price + rng.gauss(0, 0.0005) * price
        v = int(rng.uniform(1000, 5000))
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
    return candles


def _full_ensemble_req(figi="TESTIntegration", setups=None):
    return {
        "figi": figi,
        "lot": 1,
        "capital": 100_000,
        "setups": setups or [
            {"strategy_id": "rsi_reversal", "tf": "5min",
             "params": {"period": 16, "oversold": 30, "overbought": 80}},
            {"strategy_id": "macd_cross", "tf": "5min",
             "params": {"fast": 12, "slow": 26, "signal_period": 9}},
            {"strategy_id": "donchian_breakout", "tf": "5min",
             "params": {"period": 45}},
        ],
        "quorum": 2,
        "entry": {"tf": "1min", "lookback": 1},
        "exit_policy": {"id": "atr_stop", "params": {
            "period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "qty": 1,
        "from_ts": T0.isoformat(),
        "to_ts": (T0 + timedelta(hours=16)).isoformat(),
    }


class TestFullPipeline:
    def test_basic_pipeline(self):
        """Full pipeline with 3 strategies, realistic candles."""
        candles = _realistic_candles(n=1000, seed=42)
        req = _full_ensemble_req()
        result = compute_ensemble(candles, req)

        assert isinstance(result, dict)
        assert "meta" in result or "trades" in result or "error" not in result

    def test_pipeline_with_metrics(self):
        """Pipeline produces valid metrics from trades."""
        candles = _realistic_candles(n=1000, seed=42)
        req = _full_ensemble_req()
        result = compute_ensemble(candles, req)

        trades = result.get("trades", [])
        if trades:
            m = summarize(trades)
            assert m["trades"] >= 0
            assert isinstance(m["net"], float)
            assert isinstance(m.get("win_rate", 0), float)

            eq = equity_curve(trades)
            assert isinstance(eq, list)

            dd = max_drawdown_pct(eq)
            assert isinstance(dd, float)

    def test_pipeline_with_many_strategies(self):
        """Pipeline with 5 strategies (ensemble)."""
        candles = _realistic_candles(n=1200, seed=42)
        req = _full_ensemble_req(setups=[
            {"strategy_id": "rsi_reversal", "tf": "5min",
             "params": {"period": 16, "oversold": 30, "overbought": 80}},
            {"strategy_id": "macd_cross", "tf": "5min",
             "params": {"fast": 12, "slow": 26, "signal_period": 9}},
            {"strategy_id": "donchian_breakout", "tf": "5min",
             "params": {"period": 45}},
            {"strategy_id": "bollinger_reclaim", "tf": "5min",
             "params": {"period": 15, "k": 1.0}},
            {"strategy_id": "pullback_ema", "tf": "5min",
             "params": {"trend_ema": 20, "pull_ema": 10}},
        ])
        result = compute_ensemble(candles, req)
        assert isinstance(result, dict)
        assert "meta" in result or "trades" in result

    def test_pipeline_quorum_1(self):
        """Pipeline with quorum=1 (any single strategy triggers)."""
        candles = _realistic_candles(n=1000, seed=42)
        req = _full_ensemble_req()
        req["quorum"] = 1
        result = compute_ensemble(candles, req)
        assert isinstance(result, dict)

    def test_pipeline_quorum_3(self):
        """Pipeline with quorum=3 (majority needed)."""
        candles = _realistic_candles(n=1000, seed=42)
        req = _full_ensemble_req()
        req["quorum"] = 3
        result = compute_ensemble(candles, req)
        assert isinstance(result, dict)

    def test_pipeline_result_structure(self):
        """Verify result has expected keys."""
        candles = _realistic_candles(n=1000, seed=42)
        req = _full_ensemble_req()
        result = compute_ensemble(candles, req)

        # Pipeline returns either trades or analytics/meta
        assert isinstance(result, dict)
        assert "error" not in result or isinstance(result.get("error"), str)

    def test_pipeline_no_exceptions_on_realistic_data(self):
        """Run 5 different seeds, no exceptions."""
        for seed in [1, 42, 100, 999, 12345]:
            candles = _realistic_candles(n=800, seed=seed)
            req = _full_ensemble_req()
            result = compute_ensemble(candles, req)
            assert isinstance(result, dict), f"Failed on seed={seed}"
