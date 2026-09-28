"""Паритет накопительного контекста (ctx) с batch-путём compute_ensemble.

ctx обновляется инкрементально (стратегия зовёт on_bar на каждом 1m-баре), batch —
одним вызовом на всю серию. Результаты должны совпадать: те же сделки (вход/выход/
цена/причина) и та же воронка отказов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.ensemble import compute_ensemble
from app.services.ensemble_ctx import EnsembleContext

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _make_candles(n=600, base_price=100.0, seed=42):
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


def _req():
    return {
        "figi": "TEST_CTX_PARITY",
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
        "to_ts": (T0 + timedelta(hours=10)).isoformat(),
    }


def _trades_key(res):
    out = []
    for t in res.get("trades", []):
        out.append((t.get("side"), round(float(t.get("entry_price", 0)), 6),
                    round(float(t.get("exit_price", 0)), 6),
                    t.get("exit_reason"), round(float(t.get("net_pnl", 0)), 6)))
    return out


def test_ctx_matches_batch_trades():
    candles = _make_candles()
    req = _req()
    batch = compute_ensemble(list(candles), dict(req))
    assert "error" not in batch, batch.get("error")

    ctx = EnsembleContext()
    last = None
    for i in range(len(candles)):
        last = compute_ensemble(candles[:i + 1], dict(req), ctx)
    assert "error" not in last, last.get("error")
    assert _trades_key(last) == _trades_key(batch)


def test_ctx_matches_batch_funnel():
    candles = _make_candles()
    req = _req()
    batch = compute_ensemble(list(candles), dict(req))
    ctx = EnsembleContext()
    last = None
    for i in range(len(candles)):
        last = compute_ensemble(candles[:i + 1], dict(req), ctx)
    fb = (batch.get("static") or {}).get("funnel") or {}
    fc = (last.get("static") or {}).get("funnel") or {}
    for k in ("raw_signals", "quorum_unique", "entries_raw", "accepted_decisions"):
        assert fc.get(k) == fb.get(k), f"{k}: ctx={fc.get(k)} batch={fb.get(k)}"


def test_ctx_rebuild_on_reset():
    """Если буфер сброшен (len не вырос) — полный пересчёт, результат как batch."""
    candles = _make_candles()
    req = _req()
    batch = compute_ensemble(list(candles), dict(req))
    ctx = EnsembleContext()
    compute_ensemble(candles[:100], dict(req), ctx)
    last = compute_ensemble(list(candles), dict(req), ctx)
    assert _trades_key(last) == _trades_key(batch)
