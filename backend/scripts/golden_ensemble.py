"""Golden-эталон compute_ensemble на реальных данных: hash сделок + метрики.

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/golden_ensemble.py
Сверка: сравнить hash до/после оптимизаций — должен совпасть.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

V2 = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
    "donchian_breakout": {"period": 45},
    "volume_drop": {"ma_len": 20, "drop_ratio": 1.5},
}
SIDS = list(V2.keys())


async def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SMLT"
    d1 = sys.argv[2] if len(sys.argv) > 2 else "2026-07-15"
    async with SessionLocal() as db:
        figi, lot = (await db.execute(text(
            "SELECT figi, lot FROM instruments WHERE ticker = :t"), {"t": ticker})).first()
        w1 = datetime.fromisoformat(d1 + "T00:00:00+00:00")
        w2 = datetime.fromisoformat(d1 + "T23:59:00+00:00")
        candles = await _lc(db, figi, 1, date_from=w1, date_to=w2)
    print("ticker:", ticker, "candles:", len(candles))
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2[s])} for s in SIDS]
    req = {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip", "analytics": True,
        "from_ts": w1.isoformat(), "to_ts": w2.isoformat(),
    }
    t0 = time.time()
    res = compute_ensemble(candles, req)
    dt = time.time() - t0
    if "--profile" in sys.argv:
        import cProfile
        import pstats
        import io
        pr = cProfile.Profile()
        pr.enable()
        compute_ensemble(candles, req)
        pr.disable()
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18)
        print(s.getvalue()[:3200])
    st = res.get("static", {}) or {}
    trades = st.get("trades", []) or []
    key = [(t.get("entry_ts"), t.get("side"), round(float(t.get("net", 0)), 4)) for t in trades]
    h = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]
    print(f"time: {dt:.2f}s")
    print(f"trades: {len(trades)}")
    print(f"hash: {h}")
    eq = (st.get("economic") or {})
    print("net:", eq.get("net"), "gross:", eq.get("gross"), "trades:", eq.get("trades"))


asyncio.run(main())
