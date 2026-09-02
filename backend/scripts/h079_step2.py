import sys, os, json, csv, bisect, asyncio, statistics
from datetime import datetime, timezone, timedelta
sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import numpy as np

TRADES="/Users/Denis/Dev/Deeptrading/backend/reports/gold_window_202601_202607/trades.csv"
TCOST="/Users/Denis/Dev/Deeptrading/backend/reports/h079_tcost.json"
FIGIS=["BBG008F2T3T2","BBG004S681M2","BBG004S683W7","BBG004S68CP5","BBG004S681B4"]
T0=datetime(2025,12,1,tzinfo=timezone.utc); T1=datetime(2026,8,1,tzinfo=timezone.utc)
ENG="postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"

tc=json.load(open(TCOST))["per_ticker"]
GRID=np.array([1000,2500,5000,10000,25000,50000],float)
def cost_oneway_frac(figi, notional):
    d=tc.get(figi,{}).get("cost_pct_at_grid",{})
    if not d: d=json.load(open(TCOST))["aggregate_mean"]
    xs=np.array([float(k) for k in d]); ys=np.array([d[k]/100.0 for k in d])  # percent->fraction
    return float(np.interp(notional, xs, ys))

async def load_stock(figi):
    eng=create_async_engine(ENG)
    try:
        async with eng.connect() as c:
            r=await c.execute(text("SELECT ts,open,high,low,close,volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"),{"f":figi,"a":T0,"b":T1})
            rows=r.fetchall()
            if len(rows)>1000: return [(x[0].replace(tzinfo=timezone.utc),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])) for x in rows]
            r=await c.execute(text("SELECT ts,open,high,low,close,volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),{"f":figi,"a":T0,"b":T1})
            raw=[(x[0].replace(tzinfo=timezone.utc),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])) for x in r.fetchall()]
            buckets={}
            for ts,o,h,l,cl,v in raw:
                b=ts-timedelta(minutes=ts.minute%5,seconds=ts.second,microseconds=ts.microsecond)
                buckets[b]=[o,h,l,cl,v] if b not in buckets else [buckets[b][0],max(buckets[b][1],h),min(buckets[b][2],l),cl,buckets[b][4]+v]
            return [(b,)+tuple(buckets[b]) for b in sorted(buckets)]
    finally:
        await eng.dispose()

def atr14(h,l,c):
    n=len(c); out=[None]*n
    tr=[h[0]-l[0]]+[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,n)]
    for i in range(14,n): out[i]=sum(tr[i-13:i+1])/14.0
    return out

def simulate(trades, natr_lookup, stock_ts, total, tcost=False):
    avail=total*0.9; per_name_cap=0.25*total; risk_budget=0.25*total; base_size=10000.0
    by_day={}
    for t in trades: by_day.setdefault(t["dt"].date(),[]).append(t)
    taken=[]; missed=[]; max_funded=0
    for d in sorted(by_day):
        used=0.0; used_figi={f:0.0 for f in FIGIS}; risk_used=0.0; day_funded=0
        for t in sorted(by_day[d],key=lambda x:x["dt"]):
            net=t["net"]; figi=t["figi"]
            natr=natr_lookup.get((figi,t["dt"]))
            size=min(base_size,per_name_cap)
            cap_left=per_name_cap-used_figi[figi]
            if risk_budget is not None and natr:
                risk_left=risk_budget-risk_used; size=min(size, risk_left/natr if natr>0 else size)
            alloc=min(size,cap_left)
            if alloc>0 and used+alloc<=avail:
                used+=alloc; used_figi[figi]+=alloc
                if risk_budget is not None and natr: risk_used+=alloc*natr
                pnl=net*(alloc/base_size)
                if tcost:
                    cof=cost_oneway_frac(figi, alloc)
                    drag=(2*cof-0.0014)*alloc  # incremental vs baseline 0.14% roundtrip
                    pnl-=drag
                taken.append(pnl); day_funded+=1
            else:
                missed.append(net)
        max_funded=max(max_funded,day_funded)
    taken_v=[x for x in taken if x!=0]
    pos=sum(x for x in taken_v if x>0); neg=abs(sum(x for x in taken_v if x<0))
    return {"total":total,"n_taken":len(taken),"portfolio_net":round(sum(taken),2),
            "PF":round(pos/neg,3) if neg else None,"max_funded_names":max_funded}

def main():
    rows=list(csv.DictReader(open(TRADES)))
    trades=[{"figi":t["figi"],"dt":datetime.fromisoformat(t["decision_time"].replace("Z","+00:00")),"net":float(t["net_rub"])} for t in rows]
    stock_ts={}; natr_lookup={}
    for figi in FIGIS:
        bars=asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5=[b[0] for b in bars]; h=[b[2] for b in bars]; l=[b[3] for b in bars]; c=[b[4] for b in bars]
        atr=atr14(h,l,c)
        stock_ts[figi]=ts5
        for j in range(14,len(c)): natr_lookup[(figi,ts5[j]+timedelta(minutes=5))]=(atr[j]/c[j]) if (atr[j] and c[j]) else None
    for t in trades:
        figi=t["figi"]
        if figi not in stock_ts: continue
        j=bisect.bisect_right(stock_ts[figi], t["dt"]-timedelta(minutes=5))-1
        if j>=14: natr_lookup[(figi,t["dt"])]=natr_lookup.get((figi, stock_ts[figi][j]+timedelta(minutes=5)))
    out={}
    for total in (30000,50000):
        out[f"total_{total}"]={"no_tcost":simulate(trades,natr_lookup,stock_ts,total,False),
                               "with_tcost":simulate(trades,natr_lookup,stock_ts,total,True)}
    json.dump(out,open("/Users/Denis/Dev/Deeptrading/backend/reports/h079_tcost_step2.json","w"),indent=2)
    for k,v in out.items():
        print(k,"no_tcost_net=",v["no_tcost"]["portfolio_net"],"with_tcost_net=",v["with_tcost"]["portfolio_net"],
              "erosion_rub=",round(v["no_tcost"]["portfolio_net"]-v["with_tcost"]["portfolio_net"],2),
              "erosion_pct=",round(100*(v["no_tcost"]["portfolio_net"]-v["with_tcost"]["portfolio_net"])/v["no_tcost"]["portfolio_net"],1))
    print("CROSSCHECK owner net should be ~3651(30k)/7260(50k)")

if __name__=="__main__": main()
