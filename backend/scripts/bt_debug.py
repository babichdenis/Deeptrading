#!/usr/bin/env python3
"""Debug: trace exact backtest buffer for MTSS first signal."""
import asyncio
import bisect
import os, sys
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
    
    idx_entry = None
    for i, c in enumerate(ec):
        if c.ts >= aug1 and c.ts.minute % 5 == 0:
            idx_entry = i + 1
            idx_signal = i
            break
    
    entry_ts = ec[idx_entry].ts
    idx_end = bisect.bisect_right(times, entry_ts)
    
    print("Signal: idx=%d ts=%s" % (idx_signal, ec[idx_signal].ts))
    print("Entry:  idx=%d ts=%s" % (idx_entry, entry_ts))
    print("bisect_right(times, entry_ts) = %d" % idx_end)
    
    buf_raw = ec[:idx_end]
    buf_5m = resample(buf_raw[-2000:], 300)
    av = atr(buf_5m, 14)
    entry_price = ec[idx_entry].open
    
    policy = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=4.0)
    plan = policy.plan_entry(Side.BUY, entry_price, buf_5m)
    
    print("ATR(14) = %.6f" % (av[-1] if av else 0))
    print("entry=%.2f  SL=%.2f  TP=%.2f  dist=%.4f%%" % (
        entry_price, plan.stop_loss, plan.take_profit,
        abs(entry_price - plan.stop_loss) / entry_price * 100))
    
    eb = ec[idx_entry]
    print("ENTRY BAR: O=%.2f H=%.2f L=%.2f C=%.2f" % (eb.open, eb.high, eb.low, eb.close))
    
    state = PositionState.LONG
    h, r = intrabar_exit(eb, state, plan.stop_loss, plan.take_profit)
    print("intrabar_exit: %s %s" % (h, r))
    print("bar.open <= SL? %s  (%.2f <= %.2f)" % (eb.open <= plan.stop_loss, eb.open, plan.stop_loss))
    print("bar.low  <= SL? %s  (%.2f <= %.2f)" % (eb.low <= plan.stop_loss, eb.low, plan.stop_loss))
    
    # Now try ALL first 20 bars of August for MTSS
    print("\n--- Checking first 20 5min signals in August ---")
    from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
    strat = EnsembleV4Strategy(EnsembleParams(
        figi=figi, lot=10, capital=500, quorum=2,
        session="all", sessions=["morning", "day", "evening"]))
    
    cnt = 0
    for i, c in enumerate(ec):
        if c.ts < aug1 or c.ts.minute % 5 != 0:
            continue
        win = ec[max(0, i-1999):i+1]
        try:
            sig = strat.on_bar(win)
        except:
            continue
        if sig is None:
            continue
        cnt += 1
        if cnt > 10:
            break
        
        eb_idx = i + 1
        if eb_idx >= len(ec):
            continue
        entry_bar = ec[eb_idx]
        
        ie = bisect.bisect_right(times, entry_bar.ts)
        buf = ec[:ie]
        buf5 = resample(buf[-2000:], 300)
        se = Side.BUY if sig.side.value == "BUY" else Side.SELL
        plan = policy.plan_entry(se, entry_bar.open, buf5)
        st = PositionState.LONG if sig.side.value == "BUY" else PositionState.SHORT
        
        h, r = intrabar_exit(entry_bar, st, plan.stop_loss, plan.take_profit)
        sl_dist = abs(entry_bar.open - plan.stop_loss) / entry_bar.open * 100
        print("#%d %s %s entry=%.2f SL=%.2f dist=%.2f%% bar.O=%.2f bar.L=%.2f -> %s" % (
            cnt, ec[i].ts.strftime("%H:%M"), sig.side.value,
            entry_bar.open, plan.stop_loss, sl_dist,
            entry_bar.open, entry_bar.low,
            "EXIT %.2f %s" % (h, r) if h else "OK"))

asyncio.run(main())
