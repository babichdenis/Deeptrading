#!/usr/bin/env python3
"""Профиль одного прогона compute_ensemble на месяц (1 тикер), где время."""
import asyncio, sys, os, time, cProfile, pstats, io
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


async def main():
    async with SessionLocal() as db:
        figi, lot = (await db.execute(text("SELECT figi, lot FROM instruments WHERE ticker='SMLT'"))).first()
        w1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
        w2 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        candles = await _lc(db, figi, 1, date_from=w1, date_to=w2)
        print("candles:", len(candles))

    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2_PARAMS.get(s, {}))} for s in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
        "from_ts": w1.isoformat(), "to_ts": w2.isoformat(),
    }

    # timing without profile
    t0 = time.time()
    res = compute_ensemble(candles, req)
    print("plain: %.2fs" % (time.time() - t0))

    # profile
    pr = cProfile.Profile()
    pr.enable()
    res = compute_ensemble(candles, req)
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumtime")
    ps.print_stats(25)
    print(s.getvalue()[:3500])


if __name__ == "__main__":
    asyncio.run(main())
