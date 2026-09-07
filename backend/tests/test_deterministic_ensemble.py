"""Deterministic replay test for compute_ensemble.

Goal: same input candles + same req -> identical trades, equity, metrics.
If there are divergences -> non-determinism (random, dict ordering, timezone).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle
from app.services.ensemble import compute_ensemble


T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _make_candles(n=500, base_price=100.0, seed=42):
    import random
    rng = random.Random(seed)
    candles = []
    price = base_price
    for i in range(n):
        ts = T0 + timedelta(minutes=i)
        change = rng.gauss(0, 0.001) * price
        price = max(price + change, 1.0)
        o = price
        h = price * (1 + abs(rng.gauss(0, 0.0005)))
        l = price * (1 - abs(rng.gauss(0, 0.0005)))
        c = price + rng.gauss(0, 0.0003) * price
        v = int(rng.uniform(500, 2000))
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
    return candles


def _default_req():
    return {
        "figi": "TEST_DETERMINISTIC",
        "lot": 1,
        "capital": 100_000,
        "setups": [
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
        "from_ts": T0.isoformat(),
        "to_ts": (T0 + timedelta(hours=8)).isoformat(),
        "qty": 1,
    }


def _fingerprint(result):
    trades_summary = []
    for t in result.get("trades", []):
        trades_summary.append({
            "figi": t.get("figi"),
            "side": t.get("side"),
            "qty": t.get("qty"),
            "entry_price": round(float(t.get("entry_price", 0)), 6),
            "exit_price": round(float(t.get("exit_price", 0)), 6),
            "exit_reason": t.get("exit_reason"),
            "net_pnl": round(float(t.get("net_pnl", 0)), 6),
        })
    data = {
        "trades_count": len(result.get("trades", [])),
        "trades": trades_summary,
        "error": result.get("error"),
    }
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class TestDeterministicReplay:
    def test_same_input_same_output(self):
        candles = _make_candles(n=500, base_price=100.0, seed=42)
        req = _default_req()
        r1 = compute_ensemble(candles, req)
        r2 = compute_ensemble(candles, req)
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        assert fp1 == fp2, "Non-deterministic!"

    def test_same_trades_count(self):
        candles = _make_candles(n=500, seed=42)
        req = _default_req()
        r1 = compute_ensemble(candles, req)
        r2 = compute_ensemble(candles, req)
        assert len(r1.get("trades", [])) == len(r2.get("trades", []))

    def test_same_trade_details(self):
        candles = _make_candles(n=500, seed=42)
        req = _default_req()
        r1 = compute_ensemble(candles, req)
        r2 = compute_ensemble(candles, req)
        t1 = r1.get("trades", [])
        t2 = r2.get("trades", [])
        assert len(t1) == len(t2)
        for i, (a, b) in enumerate(zip(t1, t2)):
            assert a.get("entry_price") == b.get("entry_price"), f"Trade {i} entry differs"
            assert a.get("exit_price") == b.get("exit_price"), f"Trade {i} exit differs"
            assert a.get("exit_reason") == b.get("exit_reason"), f"Trade {i} reason differs"

    def test_different_seed_no_crash(self):
        candles_a = _make_candles(n=500, seed=42)
        candles_b = _make_candles(n=500, seed=99)
        req = _default_req()
        r_a = compute_ensemble(candles_a, req)
        r_b = compute_ensemble(candles_b, req)
        assert isinstance(r_a, dict)
        assert isinstance(r_b, dict)

    def test_no_crash_on_empty_candles(self):
        req = _default_req()
        result = compute_ensemble([], req)
        assert isinstance(result, dict)

    def test_no_crash_on_single_candle(self):
        candles = [Candle(ts=T0, open=100, high=101, low=99, close=100.5, volume=1000)]
        req = _default_req()
        result = compute_ensemble(candles, req)
        assert isinstance(result, dict)
