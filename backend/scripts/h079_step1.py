import sys, json
import numpy as np
sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
from app.services.research_pack import _load_candles
from datetime import datetime, timezone

FIGI = {"RUAL":"BBG008F2T3T2","SNGP":"BBG004S681M2","AFLT":"BBG004S683W7",
        "MVID":"BBG004S68CP5","NLMK":"BBG004S681B4"}
N_REF = 2500.0
COMM_BPS = 5.0  # commission 0.0005 -> 5 bps
GRID = [1000,2500,5000,10000,25000,50000]

def months():
    for m in range(1,7):
        wa=datetime(2026,m,1,tzinfo=timezone.utc)
        wb=datetime(2026,m+1,1,tzinfo=timezone.utc) if m<6 else datetime(2026,7,1,tzinfo=timezone.utc)
        yield f"2026-{m:02d}", wa, wb

def load(figi, wa, wb):
    out=[]
    for c in _load_candles(figi, wa, wb):
        out.append((int(c.ts.timestamp()), float(c.open), float(c.close), float(c.volume or 0.0)))
    out.sort()
    return out

def main():
    res={}
    for name,figi in FIGI.items():
        rows=[]
        for wn,wa,wb in months():
            rows+=load(figi,wa,wb)
        ts=np.array([r[0] for r in rows]); o=np.array([r[1] for r in rows])
        cl=np.array([r[2] for r in rows]); v=np.array([r[3] for r in rows])
        # drop zero volume bars
        m=v>0
        o,cl,v,ts=o[m],cl[m],v[m],ts[m]
        # realized gap: buy at close_t, fill next open -> use close vs next close? use open of next bar
        # approximate next-open via next bar close (1m granularity): cost proxy = |close_{t+1}-close_t|/close_t
        gap=abs(np.diff(cl))/cl[:-1]*1e4
        price=cl[:-1]; vol=v[:-1]
        p=N_REF/price/vol  # participation rate at reference notional
        p=np.clip(p,0,5.0)
        # fit |gap| ~ a + b*sqrt(p) + c*p  (Kissell transient+permanent)
        X=np.column_stack([np.ones_like(p), np.sqrt(p), p])
        beta,_,_,_=np.linalg.lstsq(X,gap,rcond=None)
        a,b,c=beta
        price_med=float(np.median(price)); vol_med=float(np.median(vol))
        curve={}
        for N in GRID:
            pn=(N/price_med)/vol_med
            pn=min(pn,5.0)
            cost_bps=a+b*np.sqrt(pn)+c*pn+COMM_BPS
            curve[str(int(N))]=round(float(cost_bps)/100,5)  # in %
        res[name]=dict(fit=dict(a=round(float(a),3),b=round(float(b),3),c=round(float(c),4)),
                       price_med=round(price_med,3), vol_med_1m=round(vol_med,1),
                       cost_pct_at_grid=curve)
    # aggregate mean curve
    agg={}
    for N in GRID:
        vals=[res[n]["cost_pct_at_grid"][str(int(N))] for n in res]
        agg[str(int(N))]=round(float(np.mean(vals)),5)
    out=dict(per_ticker=res, aggregate_mean=agg, note="Kissell-style transient+permanent fit on 2026 H1 1m bars; cost = |next-close gap| vs participation; +commission 5bps. SHADOW estimate.")
    json.dump(out, open("/Users/Denis/Dev/Deeptrading/backend/reports/h079_tcost.json","w"), indent=2)
    print(json.dumps(out, indent=2))

if __name__=="__main__": main()
