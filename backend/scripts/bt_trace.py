#!/usr/bin/env python3
"""Minimal trace: find WHY MTSS exits at 183.60 with reason=stop_loss."""
import asyncio, bisect, os, sys
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from datetime import datetime, timezone
from app.database import SessionLocal
from app.engine.exits import AtrStopPolicy, intrabar_exit
from app.engine.indicators import atr
from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.services.signals import _load_candles as _lc
from app.services.ensemble import resample
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy

async def main():
    figi = "BBG004S681W1"
    async with SessionLocal() as db:
        candles = await _lc(db, figi, 1,
            date_from=datetime(2026, 6, 2, tzinfo=timezone.utc),
            date_to=datetime(2026, 9, 1, tzinfo=timezone.utc))

    ec = [EngineCandle(ts=c.ts, open=c.open, high=c.high,
                       low=c.low, close=c.close, volume=c.volume) for c in candles]
    times = [c.ts for c in ec]
    aug1 = datetime(2026, 8, 1, tzinfo=timezone.utc)

    strat = EnsembleV4Strategy(EnsembleParams(
        figi=figi, lot=10, capital=500, quorum=2,
        session="all", sessions=["morning", "day", "evening"]))

    cnt = 0
    for i, c in enumerate(ec):
        if c.ts < aug1 or c.ts.minute % 5 != 0:
            continue
        win = ec[max(0, i-1999):i+1]
        try:
            sig = await asyncio.to_thread(strat.on_bar, win)
        except:
            continue
        if sig is None:
            continue
        cnt += 1
        if cnt > 1:
            break

        eb_idx = i + 1
        if eb_idx >= len(ec):
            continue
        eb = ec[eb_idx]

        # Reproduce EXACTLY what backtest does at entry
        ie = bisect.bisect_right(times, eb.ts)
        buf_raw = ec[:ie]
        buf5 = resample(buf_raw[-2000:], 300)
        
        policy = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=4.0)
        se = Side.BUY if sig.side.value == "BUY" else Side.SELL
        plan = policy.plan_entry(se, eb.open, buf5)
        st = PositionState.LONG if sig.side.value == "BUY" else PositionState.SHORT

        entry_price = eb.open  # this is what backtest uses
        print("=== SIGNAL at %s ===" % ec[i].ts)
        print("Entry bar: %s O=%.2f H=%.2f L=%.2f C=%.2f" % (
            eb.ts, eb.open, eb.high, eb.low, eb.close))
        print("SL=%.2f  TP=%.2f  SL_dist=%.2f%%" % (
            plan.stop_loss, plan.take_profit,
            abs(entry_price - plan.stop_loss) / entry_price * 100))

        # Now simulate what backtest does:
        # Step 1: open at candle.open, SL from plan
        current_sl = plan.stop_loss
        print("\n--- Simulating backtest loop ---")
        print("pos opened: entry=%.2f sl=%.2f" % (entry_price, current_sl))

        # Step 2: trailing stop (AtrStopPolicy.update_stop)
        new_sl = policy.update_stop(se, entry_price, current_sl, buf5)
        print("trailing: old_sl=%.2f new_sl=%.2f changed=%s" % (
            current_sl, new_sl, new_sl != current_sl))
        if new_sl != current_sl:
            current_sl = new_sl
            print("  ** SL UPDATED TO %.2f" % current_sl)

        # Step 3: intrabar_exit on entry bar
        h, r = intrabar_exit(eb, st, current_sl, plan.take_profit)
        print("intrabar on entry bar: %s %s" % (h, r))

        # Step 3 on next bars
        for j in range(eb_idx, min(eb_idx + 10, len(ec))):
            b = ec[j]
            h2, r2 = intrabar_exit(b, st, current_sl, plan.take_profit)
            tag = " <-- %s %.2f" % (r2, h2) if h2 else ""
            print("  [%d] %s O=%.2f L=%.2f -> %s%s" % (
                j-eb_idx, b.ts.strftime("%H:%M"), b.open, b.low,
                "%.2f %s" % (h2, r2) if h2 else "OK", tag))
            if h2:
                break

asyncio.run(main())
