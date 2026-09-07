import asyncio, time, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.services.signals import _load_candles as _lc
from app.database import SessionLocal
from app.engine.models import Candle as EngineCandle
from datetime import datetime, timezone, timedelta

async def main():
    async with SessionLocal() as db:
        from sqlalchemy import text
        row = (await db.execute(text("SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot, opt = row
        c1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
        c2 = datetime(2026, 8, 8, tzinfo=timezone.utc)
        candles = await _lc(db, figi, 1, date_from=c1, date_to=c2)
    # build buffer like backtest does (last 2000)
    op = opt or {}
    active = list(op.get("active_sids", ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim", "range_compression_breakout", "macd_cross", "donchian_breakout"]))
    sp = op.get("strategy_params") or {}
    from app.bot.ensemble_strategy import V2_SETUPS
    v2 = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(sp.get(s, v2.get(s, {})))} for s in active]
    strat = EnsembleV4Strategy(EnsembleParams(
        figi=figi, lot=lot, capital=10000, quorum=int(op.get("quorum", 2)),
        session="all", sessions=["morning", "day", "evening"],
        setups=setups, sl_mult=float(op.get("sl_mult", 4.0)), rr=float(op.get("rr", 4.0)),
        vol_thr=float(op.get("vol_thr", 0.0) or 0.0), neutral_mode="semi_flip",
    ))
    # call on_bar for last 2000 candles subset on 5m boundaries
    ec = [EngineCandle(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume) for c in candles]
    n = len(ec)
    t0 = time.time()
    calls = 0
    # simulate ~1 day of 5m calls (approx 250 per ticker/day)
    for i in range(n):
        if ec[i].ts.minute % 5 == 0 and i >= 50:
            sig = strat.on_bar(ec[max(0, i-2000):i+1])
            calls += 1
            if calls >= 250:
                break
    dt = time.time() - t0
    print("calls:", calls, "time: %.1fs" % dt, "=> %.3fs/call" % (dt/max(calls,1)))
    # estimate full august 20 tickers
    per_ticker_day_5m = 250
    days = 22
    tickers = 20
    est = dt / calls * per_ticker_day_5m * days * tickers
    print("estimated full aug 20 tickers: %.1f min" % (est/60))

asyncio.run(main())
