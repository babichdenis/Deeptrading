"""ЧАСТЬ 2 — ансамбль +Vortex(8-я) + ADX-фильтр, OOS 2025 (SHADOW, без изменения canonical).

S0 = canonical 7 func, quorum2 (compute_ensemble, config_hash 1c7f75dc44c2aa67) на OOS 2025.
S1 = 7+Vortex, quorum2 -> математически == S0 (Vortex не меняет кворум при quorum=2);
     честно фиксируем S1==S0, а contribution Vortex оцениваем теневым S1b (сделки S0, где
     Vortex согласован с side на entry bar) — это require-фильтр-аппроксимация.
S2 = S1b + ADX14>25 require-фильтр (пре-фильтр входа, НЕ голос).
Метрики: net, max_DD, PF, win%, coverage(n), concentration(max name net share).
OOS 2025 disjoint от H1 (2026). Деливерабл: reports/ensemble_vortex_adx.{json,md}.
"""
import sys, os, json, csv, bisect, asyncio
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENG = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"


def aggregate_5m(raw):
    buckets = {}
    for ts, o, h, l, c, v in raw:
        b = ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)
        buckets[b] = [o, h, l, c, v] if b not in buckets else [buckets[b][0], max(buckets[b][1], h), min(buckets[b][2], l), c, buckets[b][4] + v]
    return [(b,) + tuple(buckets[b]) for b in sorted(buckets)]


async def load_stock(figi):
    eng = create_async_engine(ENG)
    try:
        async with eng.connect() as c:
            r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": T0, "b": T1})
            rows = r.fetchall()
            if len(rows) > 1000:
                return [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
            r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": T0, "b": T1})
            raw = [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
            return aggregate_5m(raw)
    finally:
        await eng.dispose()


def atr14(h, l, c):
    n = len(c); out = [None] * n
    tr = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, n)]
    for i in range(14, n):
        out[i] = sum(tr[i-13:i+1]) / 14.0
    return out


def adx14(h, l, c, n=14):
    out = [None] * len(c)
    tr = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    pdi = [0.0]*len(c); mdi = [0.0]*len(c)
    for i in range(1, len(c)):
        up = h[i]-h[i-1]; dn = l[i-1]-l[i]
        pdi[i] = max(up,0) if i==1 else (pdi[i-1]*(n-1)+max(up,0))/n
        mdi[i] = max(dn,0) if i==1 else (mdi[i-1]*(n-1)+max(dn,0))/n
    pdi = [100*x/(tr[i] if tr[i] else 1) for i,x in enumerate(pdi)]
    mdi = [100*x/(tr[i] if tr[i] else 1) for i,x in enumerate(mdi)]
    dx = [100*abs(pdi[i]-mdi[i])/(pdi[i]+mdi[i]) if (pdi[i]+mdi[i]) else 0 for i in range(len(c))]
    for i in range(n*2, len(c)):
        out[i] = dx[i] if i==n*2 else (out[i-1]*(n-1)+dx[i])/n
    return out


def vortex(h, l, c, n=14):
    vip=[None]*len(c); vim=[None]*len(c)
    for i in range(1,len(c)):
        vmp=abs(h[i]-l[i-1]); vmm=abs(l[i]-h[i-1])
        tr=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
        vip[i]=vmp; vim[i]=vmm
    for i in range(n,len(c)):
        svp=sum(vip[i-n+1:i+1]); svm=sum(vim[i-n+1:i+1])
        str_=sum(max(h[j]-l[j],abs(h[j]-c[j-1]),abs(l[j]-c[j-1])) for j in range(i-n+1,i+1))
        vip[i]=svp/str_ if str_ else 0; vim[i]=svm/str_ if str_ else 0
    return vip, vim


def metrics(trades):
    if not trades:
        return {"n":0}
    nets=[t["net"] for t in trades]
    equity=[0.0]
    for x in nets: equity.append(equity[-1]+x)
    peak=equity[0]; mdd=0.0
    for x in equity:
        peak=max(peak,x); mdd=min(mdd,x-peak)
    pos=sum(x for x in nets if x>0); neg=abs(sum(x for x in nets if x<0))
    wins=sum(1 for x in nets if x>0)
    by_name={}
    for t in trades: by_name[t["figi"]]=by_name.get(t["figi"],0)+t["net"]
    total=sum(nets)
    conc=max(abs(v) for v in by_name.values())/abs(total) if total else 0
    return {"n":len(trades),"net":round(sum(nets),2),"max_DD":round(mdd,2),
            "PF":round(pos/neg,3) if neg else None,"win_pct":round(100*wins/len(trades),1),
            "coverage":len(trades),"concentration":round(100*conc,1)}


def main():
    import numpy as np
    from app.services.research_pack import _load_candles
    from app.services.ensemble import compute_ensemble
    OUT=os.path.join(REPORTS,"oos2025_vortex_adx")
    os.makedirs(OUT, exist_ok=True)
    base_req = lambda figi: {'figi':figi,'bias_mode':'info','bias':{'tf':'hour','period':50},
        'entry_tf':'5min','entry':{'tf':'5min','lookback':1},'entry_session':'main','quorum':2,
        'same_side_reentry_cooldown_bars':15,'carry_overnight':True,'opposite_hold':False,
        'exit_policy':{'id':'atr_stop','params':{'period':14,'multiplier':2.0,'risk_reward':2.0}},
        'commission_rate':0.0005,'slippage_bps':2.0,'capital':10000.0,'lot':10,
        'use_all_setups':True,'drop_useless':True,'from_ts':T0.isoformat(),'to_ts':T1.isoformat()}
    s0=[]; vortex_adx={}
    for figi in FIGIS:
        candles=_load_candles(figi, T0, T1)
        print(f"  {figi}: {len(candles)} candles", flush=True)
        res=compute_ensemble(candles, base_req(figi))
        if 'error' in res:
            print(f"  {figi} err: {res['error']}", flush=True); continue
        ts=res['static']['trades']
        for t in ts:
            s0.append({'figi':figi,'side':t.get('side'),'entry_ts':t['entry_ts'],'net':float(t['net'])})
        print(f"  {figi}: {len(ts)} trades", flush=True)
        # 5m для Vortex/ADX
        bars=asyncio.new_event_loop().run_until_complete(load_stock(figi))
        ts5=[b[0] for b in bars]; h=[b[2] for b in bars]; l=[b[3] for b in bars]; c=[b[4] for b in bars]
        vip,vim=vortex(h,l,c); ad=adx14(h,l,c)
        d={}
        for j in range(14,len(c)):
            d[ts5[j]+timedelta(minutes=5)]=(vip[j],vim[j],ad[j])
        vortex_adx[figi]=d
    print(f"S0 trades: {len(s0)}", flush=True)
    # разметка Vortex/ADX по entry (last-closed bar)
    s1b=[]; s2=[]
    for t in s0:
        figi=t['figi']; dt=datetime.fromisoformat(t['entry_ts'].replace('Z','+00:00'))
        d=vortex_adx.get(figi, {})
        ts_list=sorted(d)
        jj=bisect.bisect_right(ts_list, dt-timedelta(minutes=5))-1
        if jj<0: continue
        vip,vim,ad=d[ts_list[jj]]
        if vip is None or ad is None: continue
        vortex_agree = (t['side']=='BUY' and vip>vim) or (t['side']=='SELL' and vim>vip)
        if vortex_agree: s1b.append(t)
        if vortex_agree and ad>25: s2.append(t)
    m0=metrics(s0); m1=metrics(s1b); m2=metrics(s2)
    crit = {
        "net_ge_0.95_S0": m1["net"]>=0.95*m0["net"] if m0.get("net") else None,
        "DD_le_0.80_S0": (m1["max_DD"]<=0.80*m0["max_DD"]) if (m0.get("max_DD") and m0["max_DD"]<0) else None,
        "PF_ge_S0": (m1["PF"]>=m0["PF"]) if (m0.get("PF") and m1.get("PF")) else None,
        "win_ge_S0": (m1["win_pct"]>=m0["win_pct"]) if (m0.get("win_pct") and m1.get("win_pct")) else None,
        "coverage_ge_95pct": (m1["n"]>=0.95*m0["n"]) if m0.get("n") else None,
        "concentration_le_50pct": (m1["concentration"]<=50) if m1.get("concentration") is not None else None,
    }
    out={"schema":"ensemble_vortex_adx","window":"OOS 2025 (2025-01..2026-01)","baseline_hash":"1c7f75dc44c2aa67",
         "note":"S1(7+Vortex,quorum2)==S0 математически; S1b=Vortex-подтверждённые (теневой require-фильтр); S2=S1b+ADX>25",
         "S0_canonical":m0,"S1b_vortex_confirmed":m1,"S2_vortex_adx":m2,"criteria_S1b_vs_S0":crit}
    json.dump(out, open(os.path.join(REPORTS,"ensemble_vortex_adx.json"),"w"), ensure_ascii=False, indent=2)
    print("S0",m0); print("S1b",m1); print("S2",m2); print("crit",crit)
    print("saved ensemble_vortex_adx.json", flush=True)


if __name__=="__main__":
    main()
