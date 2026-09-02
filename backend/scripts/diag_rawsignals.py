import sys, json
from datetime import datetime, timezone
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from app.services.ensemble import compute_ensemble
from app.services.research_pack import _load_candles

BASE = dict(bias_mode="info", bias=dict(tf="hour", period=50), entry_tf="5min",
    entry=dict(tf="5min", lookback=1), entry_session="main", quorum=2,
    same_side_reentry_cooldown_bars=15, carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True)

figi="BBG008F2T3T2"
a=datetime(2025,1,1,tzinfo=timezone.utc); b=datetime(2025,2,1,tzinfo=timezone.utc)
req=dict(BASE); req["figi"]=figi; req["from_ts"]=a.isoformat(); req["to_ts"]=b.isoformat()
candles=_load_candles(figi,a,b)
r=compute_ensemble(candles,req)
st=r["static"]
rs=st["funnel"]["raw_signals"]
print("raw_signals type:", type(rs), "len:", len(rs) if hasattr(rs,'__len__') else '?')
if isinstance(rs, list) and rs:
    print("raw_signals[0]:", json.dumps(rs[0], default=str)[:600])
    print("raw_signals[1] keys:", list(rs[1].keys()) if isinstance(rs[1],dict) else type(rs[1]))
elif isinstance(rs, dict):
    print("raw_signals dict keys sample:", list(rs.keys())[:5])
    k0=list(rs.keys())[0]
    print("raw_signals[",k0,"]:", json.dumps(rs[k0], default=str)[:600])
print("--- entries[0] ---")
en=st["entries"]
print("entries len:", len(en), "entries[0]:", json.dumps(en[0], default=str)[:600] if en else None)
print("--- quorum_list[0] ---")
ql=st["quorum_list"]
print("quorum_list len:", len(ql), "q[0]:", json.dumps(ql[0], default=str)[:400] if ql else None)
