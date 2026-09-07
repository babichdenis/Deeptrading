import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim","range_compression_breakout","macd_cross","donchian_breakout"]
V2P = {
    "rsi_reversal": {"period":16,"oversold":30,"overbought":80},"bollinger_reclaim":{"period":15,"k":1.0},
    "pullback_ema":{"trend_ema":20,"pull_ema":10},"range_compression_breakout":{"lookback":15,"atr_period":16,"pct":40.0},
    "donchian_breakout":{"period":45},"vwap_reclaim":{"k":2.0},"macd_cross":{"fast":12,"slow":26,"signal_period":9}}

async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot, opt = row
    # ТОЛЬКО w1 10-17 авг, как в optuna (без длинного warmup)
    c = await _lc(db, figi, 1, date_from=datetime(2026,8,10,tzinfo=timezone.utc), date_to=datetime(2026,8,17,tzinfo=timezone.utc))
    def run(sids, sp, q, vol, fr, to, nm="semi_flip"):
        setups=[{"strategy_id":s,"tf":"5min","params":dict(sp.get(s,V2P.get(s,{})))} for s in sids]
        req={"figi":figi,"bias_mode":"info","bias":{"tf":"hour","period":50},"entry_tf":"5min","entry":{"tf":"5min","lookback":1},
             "entry_session":"main","quorum":q,"same_side_reentry_cooldown_bars":15,"carry_overnight":True,"opposite_hold":False,"confirm_flip":2,
             "exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":float(opt.get("sl_mult",4.0)),"risk_reward":float(opt.get("rr",4.0))}},
             "commission_rate":0.0005,"slippage_bps":2.0,"capital":10000,"lot":lot,"setups":setups,"use_all_setups":False,"drop_useless":True,
             "neutral_mode":nm,"from_ts":fr.isoformat(),"to_ts":to.isoformat()}
        if vol>0: req["volume_filter_threshold"]=vol
        res=compute_ensemble(c,req)
        st=res.get("static",{})
        tr=st.get("trades",[])
        ec=st.get("economic",{}) or {}
        return len(tr), ec.get("net")
    sp_opt = opt.get("strategy_params") or {}
    w1_fr, w1_to = datetime(2026,8,10,tzinfo=timezone.utc), datetime(2026,8,17,tzinfo=timezone.utc)
    print("optuna params on w1-only:", run(list(opt.get("active_sids",ALL)), sp_opt, int(opt.get("quorum",2)), float(opt.get("vol_thr",0) or 0), w1_fr, w1_to))
    print("V2 all-7 on w1-only:", run(ALL, V2P, 2, 0, w1_fr, w1_to))

asyncio.run(main())
