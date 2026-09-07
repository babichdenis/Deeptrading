#!/usr/bin/env python3
"""Minimal debug: check first few MTSS signals for intrabar_exit."""
import asyncio, bisect, os, sys
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from datetime import datetime, timezone
from app.database import SessionLocal
from app.engine.exits import AtrStopPolicy, intrabar_exit
from app.engine.indicators import atr
from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.services.signals import _load_candles as _lc
from app.services.ensemble import resample

async def main():
    figi = "BBG004S681W1"
    async with SessionLocal() as db:
        candles = await _lc(db, figi, 1,
            date_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
            date_to=datetime(2026, 9, 1, tzinfo=timezone.utc))

    ec = [EngineCandle(ts=c.ts, open=c.open, high=c.high,
                       low=c.low, close=c.close, volume=c.volume) for c in candles]
    times = [c.ts for c in ec]
    aug1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    policy = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=4.0)

    cnt = 0
    for i, c in enumerate(ec):
        if c.ts < aug1 or c.ts.minute % 5 != 0:
            continue
        cnt += 1
        if cnt > 5:
            break
        eb_idx = i + 1
        if eb_idx >= len(ec):
            continue
        eb = ec[eb_idx]

        ie = bisect.bisect_right(times, eb.ts)
        buf = ec[:ie]
        buf5 = resample(buf[-2000:], 300)
        plan = policy.plan_entry(Side.BUY, eb.open, buf5)
        st = PositionState.LONG

        h, r = intrabar_exit(eb, st, plan.stop_loss, plan.take_profit)
        sl_dist = abs(eb.open - plan.stop_loss) / eb.open * 100
        print("#%d signal=%s entry_bar=%s O=%.2f L=%.2f SL=%.2f dist=%.2f%% -> %s" % (
            cnt, c.ts.strftime("%H:%M"), eb.ts.strftime("%H:%M"),
            eb.open, eb.low, plan.stop_loss, sl_dist,
            "EXIT %.2f %s" % (h, r) if h else "OK"))

asyncio.run(main())
