import asyncio, time, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.services.signals import _load_candles as _lc
from app.database import SessionLocal
from app.engine.models import Candle as EngineCandle
from sqlalchemy import text
from datetime import datetime, timezone

async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot, opt = row
        candles = await _lc(db, figi, 1, date_from=datetime(2026,8,1,tzinfo=timezone.utc), date_to=datetime(2026,8,8,tzinfo=timezone.utc))
    op = opt or {}
    active = list(op.get("active_sids", []))
    if not active: active = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim","range_compression_breakout","macd_cross","donchian_breakout"]
    sp = op.get("strategy_params") or {}
    from app.bot.ensemble_strategy import V2_SETUPS
    v2 = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(sp.get(s, v2.get(s, {})))} for s in active]
    strat = EnsembleV4Strategy(EnsembleParams(
        figi=figi, lot=lot, capital=10000, quorum=int(op.get("quorum",2)),
        session="all", sessions=["morning","day","evening"],
        setups=setups, sl_mult=float(op.get("sl_mult",4.0)), rr=float(op.get("rr",4.0)),
        vol_thr=float(op.get("vol_thr",0.0) or 0.0), neutral_mode="semi_flip",
    ))
    ec = [EngineCandle(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume) for c in candles]
    # instrument by patching on_bar to count how far
    import app.bot.ensemble_strategy as es_mod
    orig = es_mod.EnsembleV4Strategy.on_bar
    counter = {"calls":0, "reached_ensemble":0, "signals":0, "returns":{}}
    def patched(self, candles):
        counter["calls"] += 1
        r = orig(self, candles)
        if r is not None:
            counter["signals"] += 1
        else:
            pass
        return r
    es_mod.EnsembleV4Strategy.on_bar = patched
    t0 = time.time()
    for i in range(len(ec)):
        if ec[i].ts.minute % 5 == 0 and i >= 50:
            strat.on_bar(ec[max(0,i-2000):i+1])
    dt = time.time()-t0
    print("total bars:", len(ec))
    print("calls:", counter["calls"], "signals:", counter["signals"])
    print("time %.2fs => %.3fs/call" % (dt, dt/max(counter["calls"],1)))
    per_day = 250; days=22; tk=20
    print("est full aug 20tk: %.0f min" % ((dt/max(counter["calls"],1))*per_day*days*tk/60))

asyncio.run(main())
