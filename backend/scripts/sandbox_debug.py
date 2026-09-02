import sys, json
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from datetime import datetime, timezone
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

figi = "BBG008F2T3T2"
t_from = datetime(2026,5,1,tzinfo=timezone.utc)
t_to = datetime(2026,5,10,tzinfo=timezone.utc)
candles = _load_candles(figi, t_from, t_to)
print(f"Candles: {len(candles)}")

req = {"figi":figi,"bias_mode":"info","bias":{"tf":"hour","period":50},
       "entry_tf":"5min","entry":{"tf":"5min","lookback":1},"entry_session":"all",
       "quorum":2,"same_side_reentry_cooldown_bars":15,"carry_overnight":True,"opposite_hold":False,
       "exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":2.0,"risk_reward":2.0}},
       "commission_rate":0.0005,"slippage_bps":2.0,"capital":10000.0,"lot":10,
       "use_all_setups":True,"drop_useless":True,
       "from_ts":t_from.isoformat(),"to_ts":t_to.isoformat()}
res = compute_ensemble(candles, req)
stt = res["static"]
econ = stt["economic"]
print(f"Keys in economic: {list(econ.keys())}")
trades = econ["trades"]
print(f"Type of trades: {type(trades)}")
if isinstance(trades, list):
    print(f"Trades count: {len(trades)}")
    if trades:
        print(f"First trade keys: {list(trades[0].keys())}")
        print(json.dumps(trades[0], indent=2, default=str))
elif isinstance(trades, dict):
    print(f"Trades dict keys: {list(trades.keys())}")
else:
    print(f"Trades value: {trades}")
