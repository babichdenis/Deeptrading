import sys, json
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from bisect import bisect_right
from datetime import datetime, timezone
import numpy as np
from app.services.research_pack import _load_candles
from app.services.ensemble import compute_ensemble

FIGIS = {"BBG008F2T3T2":"RUAL","BBG004S681M2":"SNGP","BBG004S683W7":"AFLT",
         "BBG004S68CP5":"MVID","BBG004S681B4":"NLMK"}
BASE = dict(bias_mode="info", bias=dict(tf="hour", period=50), entry_tf="5min",
    entry=dict(tf="5min", lookback=1), entry_session="main", quorum=2,
    same_side_reentry_cooldown_bars=15, carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True)

def _months():
    ms=[]; y,mo=2025,1
    while (y,mo)<=(2026,6):
        a=datetime(y,mo,1,tzinfo=timezone.utc)
        b=datetime(y+1,1,1,tzinfo=timezone.utc) if mo==12 else datetime(y,mo+1,1,tzinfo=timezone.utc)
        ms.append((f"{y}-{mo:02d}",a,b))
        if mo==12: y,mo=y+1,1
        else: mo+=1
    return ms

def _label(wn):
    y,m=wn.split("-"); m=int(m)
    if y=="2025" and m<=6: return "2025-H1"
    if y=="2025": return "2025-H2"
    return "2026-H1"

def resample_5min(ts_arr, high, low, close):
    bars=[]; cur=None; ch=[]; cl=[]; cc=[]
    for t,h,l,c in zip(ts_arr, high, low, close):
        bucket = int(t)//300*300
        if cur is None: cur=bucket
        if bucket!=cur:
            bars.append((cur, max(ch), min(cl), cc[-1])); cur=bucket; ch=[]; cl=[]; cc=[]
        ch.append(h); cl.append(l); cc.append(c)
    if ch: bars.append((cur, max(ch), min(cl), cc[-1]))
    if not bars: return np.array([]),np.array([]),np.array([]),np.array([])
    return (np.array([b[0] for b in bars]), np.array([b[1] for b in bars],float),
            np.array([b[2] for b in bars],float), np.array([b[3] for b in bars],float))

def donchian_high(h, p):
    n=len(h); out=np.full(n,np.nan)
    for i in range(p, n): out[i]=np.max(h[i-p:i])
    return out

def donchian_low(l, p):
    n=len(l); out=np.full(n,np.nan)
    for i in range(p, n): out[i]=np.min(l[i-p:i])
    return out

def simulate(deal, bts, bh, bl, bc):
    try:
        ets=datetime.fromisoformat(deal["entry_ts"].replace("Z","+00:00")).timestamp()
        xts=datetime.fromisoformat(deal["exit_ts"].replace("Z","+00:00")).timestamp()
    except Exception:
        return None
    ei=bisect_right(bts, ets); xi=bisect_right(bts, xts)
    if xi-ei<2: return None
    h=bh[ei:xi]; l=bl[ei:xi]; c=bc[ei:xi]
    side=1 if deal["side"] in ("BUY","LONG","BULL") else -1
    entry_px=float(deal["entry_px"]); exit_px=float(deal["exit_px"])
    base_net=float(deal["net"])
    base_notional=float(deal.get("entry_notional", abs(base_net))) or 1.0
    dc126=donchian_high(h,126) if side==1 else donchian_low(l,126)
    dc252=donchian_high(h,252) if side==1 else donchian_low(l,252)
    units=[entry_px]; a126=False; a252=False
    for i in range(1,len(h)):
        if i>=126 and not a126 and not np.isnan(dc126[i]):
            if (side==1 and h[i]>dc126[i]) or (side==-1 and l[i]<dc126[i]):
                units.append(c[i]); a126=True
        if i>=252 and not a252 and not np.isnan(dc252[i]):
            if (side==1 and h[i]>dc252[i]) or (side==-1 and l[i]<dc252[i]):
                units.append(c[i]); a252=True
    pyr_net=sum((exit_px-ep)*side for ep in units)
    n_units=len(units)
    pyr_notional=base_notional*n_units
    return dict(base_net=base_net, base_notional=base_notional, pyr_net=pyr_net,
                pyr_notional=pyr_notional, n_units=n_units,
                bars=int(deal.get("bars_held", len(h))),
                right_base=1 if base_notional>0 and base_net/base_notional>1.0 else 0,
                right_pyr=1 if pyr_notional>0 and pyr_net/pyr_notional>1.0 else 0,
                entry_ts=str(deal.get("entry_ts")), exit_ts=str(deal.get("exit_ts")))

def main():
    figi=sys.argv[1]; sym=FIGIS[figi]; rows=[]
    for wname,wa,wb in _months():
        candles=_load_candles(figi, wa, wb)
        if len(candles)<100:
            print(f"  {sym} {wname}: skip few {len(candles)}", flush=True); continue
        req=dict(BASE); req["figi"]=figi; req["from_ts"]=wa.isoformat(); req["to_ts"]=wb.isoformat()
        out=compute_ensemble(candles, req)
        trades=out.get("static",{}).get("trades",[])
        ts_arr=np.array([c.ts.timestamp() for c in candles])
        high=np.array([c.high for c in candles], float)
        low=np.array([c.low for c in candles], float)
        close=np.array([c.close for c in candles], float)
        bts,bh,bl,bc=resample_5min(ts_arr, high, low, close)
        if len(bts)==0: continue
        for d in trades:
            r=simulate(d, bts, bh, bl, bc)
            if r is None: continue
            r["window"]=_label(wname); r["figi"]=sym
            rows.append(r)
        print(f"  {sym} {wname}: trades={len(trades)} rows={len(rows)}", flush=True)
    json.dump(rows, open(f"reports/_h082_{sym}.json","w"))
    print(f"WROTE reports/_h082_{sym}.json rows={len(rows)}", flush=True)

if __name__=="__main__":
    main()

