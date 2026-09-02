import sys, json
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import asyncio
from app.services.ensemble import compute_ensemble
from app.services.research_pack import _load_candles
from app.config import get_settings

BASE = dict(bias_mode="info", bias=dict(tf="hour", period=50), entry_tf="5min",
    entry=dict(tf="5min", lookback=1), entry_session="main", quorum=2,
    same_side_reentry_cooldown_bars=15, carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True)

def aggregate_5m(raw):
    buckets = {}
    for ts, o, h, l, c, v in raw:
        b = ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)
        buckets[b] = [o, h, l, c, v] if b not in buckets else [buckets[b][0], max(buckets[b][1], h), min(buckets[b][2], l), c, buckets[b][4] + v]
    return [(b,) + tuple(buckets[b]) for b in sorted(buckets)]

def load_stock(figi, a, b, eng_url):
    async def _do():
        eng = create_async_engine(eng_url)
        try:
            async with eng.connect() as c:
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": a, "b": b})
                rows = r.fetchall()
                if len(rows) > 1000:
                    return [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": a, "b": b})
                raw = [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
                return aggregate_5m(raw)
        finally:
            await eng.dispose()
    return asyncio.new_event_loop().run_until_complete(_do())

def vortex(h, l, c, p=14):
    ph = np.array(h, float); pl = np.array(l, float); pc = np.array(c, float)
    tr = np.maximum.reduce([np.abs(ph[1:] - pc[:-1]), np.abs(pl[1:] - pc[:-1]), ph[1:] - pl[1:]])
    up = np.abs(ph[1:] - pl[:-1]); dn = np.abs(pl[1:] - ph[:-1])
    vip = np.full(len(pc), np.nan); vim = np.full(len(pc), np.nan)
    for i in range(p, len(pc)):
        trs = tr[i - p:i].sum(); ups = up[i - p:i].sum(); dns = dn[i - p:i].sum()
        if trs > 0:
            vip[i] = ups / trs; vim[i] = dns / trs
    return vip, vim

figi = "BBG008F2T3T2"
a = datetime(2025,1,1,tzinfo=timezone.utc); b = datetime(2025,2,1,tzinfo=timezone.utc)
req = dict(BASE); req["figi"]=figi; req["from_ts"]=a.isoformat(); req["to_ts"]=b.isoformat()
candles = _load_candles(figi, a, b)
r = compute_ensemble(candles, req)
trades = r.get("static", {}).get("trades", [])
print("n trades jan:", len(trades))
print("first 3 entry_ts:", [t["entry_ts"] for t in trades[:3]])
bars = load_stock(figi, a, b, get_settings().database_url)
print("n 5m bars:", len(bars), "first bar ts:", bars[0][0], "last:", bars[-1][0])
ts5 = [x[0] for x in bars]; h=[x[1] for x in bars]; l=[x[2] for x in bars]; c=[x[3] for x in bars]
vip, vim = vortex(h,l,c)
print("vip[:16]:", [None if np.isnan(x) else round(x,2) for x in vip[:16]])
print("vim[:16]:", [None if np.isnan(x) else round(x,2) for x in vim[:16]])
# state agree count
d = {}
for j in range(14, len(c)):
    d[ts5[j]+timedelta(minutes=5)] = (vip[j], vim[j])
ts_list = sorted(d)
state_ok=0; cross_ok=0
for t in trades:
    dt = datetime.fromisoformat(t["entry_ts"].replace("Z","+00:00"))
    jj = bisect_right(ts_list, dt - timedelta(minutes=5)) - 1
    if jj < 0:
        continue
    vp, vm = d[ts_list[jj]]
    if vp is None: continue
    if (t["side"]=="BUY" and vp>vm) or (t["side"]=="SELL" and vm>vp):
        state_ok += 1
    if jj>=1:
        vp1, vm1 = d[ts_list[jj-1]]
        if t["side"]=="BUY" and vp>vm and vp1<=vm1: cross_ok+=1
        if t["side"]=="SELL" and vm>vp and vm1<=vp1: cross_ok+=1
print("state_ok:", state_ok, "cross_ok:", cross_ok, "of", len(trades))
