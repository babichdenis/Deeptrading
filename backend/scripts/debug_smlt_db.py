import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot, opt = row
    print("=== optuna_params IN DB ===")
    print(json.dumps(opt, indent=1)[:800])
    c = await _lc(db, figi, 1, date_from=datetime(2026,8,10,tzinfo=timezone.utc), date_to=datetime(2026,8,17,tzinfo=timezone.utc))
    sp = opt.get("strategy_params") or {}
    active = opt.get("active_sids") or []
    setups=[{"strategy_id":s,"tf":"5min","params":dict(sp.get(s,{}))} for s in active]
    print("setups:", setups)
    req={"figi":figi,"bias_mode":"info","bias":{"tf":"hour","period":50},"entry_tf":"5min","entry":{"tf":"5min","lookback":1},
         "entry_session":"main","quorum":int(opt.get("quorum",2)),"same_side_reentry_cooldown_bars":15,"carry_overnight":True,"opposite_hold":False,"confirm_flip":2,
         "exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":float(opt.get("sl_mult",4.0)),"risk_reward":float(opt.get("rr",4.0))}},
         "commission_rate":0.0005,"slippage_bps":2.0,"capital":10000,"lot":lot,"setups":setups,"use_all_setups":False,"drop_useless":True,
         "neutral_mode":"semi_flip","from_ts":datetime(2026,8,10,tzinfo=timezone.utc).isoformat(),"to_ts":datetime(2026,8,17,tzinfo=timezone.utc).isoformat()}
    v=float(opt.get("vol_thr",0) or 0)
    if v>0: req["volume_filter_threshold"]=v
    res=compute_ensemble(c,req)
    if "error" in res: print("ERROR", res["error"]); return
    st=res.get("static",{})
    print("trades:", len(st.get("trades",[])))
    rej = st.get("rejected",[])
    cnt = Counter(str(r.get("reason",""))[:40] for r in rej)
    print("rejected top:", cnt.most_common(8))
    ent = st.get("entries",[])
    print("entries:", len(ent))
    # quorum_list size
    print("quorum_list:", len(st.get("quorum_list",[])))

asyncio.run(main())
