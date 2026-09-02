import sys; sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from app.services.ensemble import compute_ensemble
from app.engine.models import Candle
from datetime import datetime
import psycopg2
DB="host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading password=deeptrading"
conn=psycopg2.connect(DB); cur=conn.cursor()
cur.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE figi=%s AND interval=1 ORDER BY ts LIMIT 300", ("BBG008F2T3T2",))
rows=cur.fetchall(); cur.close(); conn.close()
candles=[Candle(ts=r[0],open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5])) for r in rows]
req={"figi":"BBG008F2T3T2","bias_mode":"info","bias":{"tf":"hour","period":50},"entry_tf":"5min","entry":{"tf":"5min","lookback":1},"entry_session":"main","quorum":2,"same_side_reentry_cooldown_bars":15,"carry_overnight":True,"opposite_hold":False,"exit_policy":{"id":"atr_stop","params":{"period":14,"multiplier":2.0,"risk_reward":2.0}},"commission_rate":0.0005,"slippage_bps":2.0,"capital":10000.0,"lot":10,"use_all_setups":True,"drop_useless":True,"from_ts":"2026-07-01T00:00:00Z","to_ts":"2026-07-31T23:59:59Z"}
res=compute_ensemble(candles,req)
print("ERROR:", res.get("error", "none"))
print("BARS:", res.get("bars", 0))
print("KEYS:", list(res.keys()))
