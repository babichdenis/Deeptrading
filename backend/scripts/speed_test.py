#!/usr/bin/env python3
"""Замер скорости одного прогона compute_ensemble на 1 неделю."""
import asyncio, sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]
V2_PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}


def base_req(figi, lot, mult=4.0, rr=4.0, quorum=2, vol_thr=None, neutral="semi_flip"):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2_PARAMS.get(s, {}))} for s in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": quorum, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": mult, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
    }
    if neutral:
        req["neutral_mode"] = neutral
    if vol_thr:
        req["volume_filter_threshold"] = vol_thr
    return req


async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot = row
        w1 = datetime(2026, 8, 10, tzinfo=timezone.utc)
        w2 = datetime(2026, 8, 17, tzinfo=timezone.utc)
        candles = await _lc(db, figi, 1, date_from=w1, date_to=w2)
        print("candles:", len(candles))

    req = base_req(figi, lot)
    req["from_ts"] = datetime(2026, 8, 10, tzinfo=timezone.utc).isoformat()
    req["to_ts"] = datetime(2026, 8, 17, tzinfo=timezone.utc).isoformat()
    t0 = time.time()
    res = compute_ensemble(candles, req)
    dt = time.time() - t0
    if "error" in res:
        print("ERROR:", res["error"])
        return
    st = res.get("static", {})
    tr = st.get("trades", [])
    ec = st.get("economic", {})
    print("time: %.1fs trades:%d net:%s" % (dt, len(tr), ec.get("net") if ec else None))
    if ec:
        print("economic keys:", list(ec.keys()))


if __name__ == "__main__":
    asyncio.run(main())
