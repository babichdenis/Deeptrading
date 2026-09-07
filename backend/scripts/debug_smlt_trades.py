import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot = row
        c = await _lc(db, figi, 1, date_from=datetime(2026,8,1,tzinfo=timezone.utc), date_to=datetime(2026,9,1,tzinfo=timezone.utc))
    # baseline V2 без semi_flip, все 7 стратегий
    ALL = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim","range_compression_breakout","macd_cross","donchian_breakout"]
    V2P = {"rsi_reversal":{"period":16,"oversold":30,"overbought":80},"bollinger_reclaim":{"period":15,"k":1.0},"pullback_ema":{"trend_ema":20,"pull_ema":10},"range_compression_breakout":{"lookback":15,"atr_period":16,"pct":40.0},"donchian_breakout":{"period":45},"vwap_reclaim":{"k":2.0},"macd_cross":{"fast":12,"slow":26,"signal_period":9}}
    setups=[{"strategy_id":s,"tf":"5min","params":dict(V2P[s])} for s in ALL]
    req={"figi":figi,"bias_mode":"info","bias":{"tf":"hour","period":50},"entry_tf":"5min","entry":{"tf":"5min","lookback":1},"entry_session":"main","quorum":2,
         "same_side_reentry_cooldown_bars":15,"carry_overnight":True,"opposite_hold":False,"confirm_flip":2,
         "exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":4.0,"risk_reward":4.0}},"commission_rate":0.0005,"slippage_bps":2.0,
         "capital":10000,"lot":lot,"setups":setups,"use_all_setups":False,"drop_useless":True,
         "from_ts":datetime(2026,8,1,tzinfo=timezone.utc).isoformat(),"to_ts":datetime(2026,9,1,tzinfo=timezone.utc).isoformat()}
    res = compute_ensemble(c, req)
    st = res.get("static", {})
    trades = st.get("trades", [])
    entries = st.get("entries", [])
    print("baseline trades:", len(trades), "entries:", len(entries))
    # уникальные entry_ts в trades
    uniq = len(set(t["entry_ts"] for t in trades))
    print("unique entry_ts:", uniq)
    print("episodes? check first trades")
    for t in trades[:5]:
        print("  ", t["entry_ts"], t["side"], "->", t["exit_ts"], t["exit_reason"])

asyncio.run(main())
