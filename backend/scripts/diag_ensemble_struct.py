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
print("TOP keys:", list(r.keys()))
for k in r:
    v=r[k]
    if isinstance(v, dict):
        print(f"  r[{k}] dict keys:", list(v.keys())[:30])
    elif isinstance(v,(list,)):
        print(f"  r[{k}] list len:", len(v))
    else:
        print(f"  r[{k}]:", type(v).__name__)
if "static" in r:
    st=r["static"]
    print("static keys:", list(st.keys()))
    for k in st:
        v=st[k]
        if isinstance(v, dict): print(f"  static[{k}] dict keys:", list(v.keys())[:30])
        elif isinstance(v, list): print(f"  static[{k}] list len:", len(v))
        else: print(f"  static[{k}]:", type(v).__name__)
# look for votes/signals in trades
tr=st.get("trades",[])
print("n trades:", len(tr))
if tr:
    print("trade keys:", list(tr[0].keys()))
    for key in ["votes","signals","vote","signal_strength","strategies","contributions"]:
        if key in tr[0]: print("  FOUND trade[",key,"]:", str(tr[0][key])[:200])
