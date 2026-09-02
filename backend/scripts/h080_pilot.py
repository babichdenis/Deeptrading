import sys, json
sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble
from datetime import datetime, timezone

figi="BBG004730N88"  # SBER
req=dict(figi=figi, from_ts="2026-01-01T00:00:00+00:00", to_ts="2026-07-01T00:00:00+00:00",
         use_all_setups=True, capital=10000.0, lot=10, quorum=2,
         exit_policy={"id":"atr_stop","params":{"period":14,"multiplier":2.0,"risk_reward":2}})
cs=list(_load_candles(figi, datetime(2026,1,1,tzinfo=timezone.utc), datetime(2026,7,1,tzinfo=timezone.utc)))
print("bars", len(cs), flush=True)
res=compute_ensemble(cs, req)
print("TOP KEYS:", list(res.keys()), flush=True)
print("ORACLE KEYS:", list(res["oracle"].keys()), flush=True)
print("oracle trades COUNT:", res["oracle"]["trades"], flush=True)
print("STATIC KEYS:", list(res["static"].keys()), flush=True)
print("STATIC ECON:", res["static"]["economic"], flush=True)
print("COMP:", res["comparison"], flush=True)
