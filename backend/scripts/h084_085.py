import sys, json, glob
import numpy as np
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
from app.services.research_pack import _load_candles
from datetime import datetime, timezone

FIGIS = {"BBG008F2T3T2":"RUAL","BBG004S681M2":"SNGP","BBG004S683W7":"AFLT",
         "BBG004S68CP5":"MVID","BBG004S681B4":"NLMK"}

def _months():
    for y in (2025,2026):
        for m in range(1,13):
            if (y,m) > (2026,6): break
            if (y,m) < (2025,1): continue
            wa=datetime(y,m,1,tzinfo=timezone.utc)
            wb=datetime(y+1,1,1,tzinfo=timezone.utc) if m==12 else datetime(y,m+1,1,tzinfo=timezone.utc)
            yield f"{y}-{m:02d}", wa, wb

def load_1m(figi, wa, wb):
    cs=_load_candles(figi, wa, wb)
    return [(int(c.ts.timestamp()), float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)) for c in cs]

def ema(a, n):
    a=np.asarray(a,float); out=np.empty_like(a); out[:]=np.nan
    if len(a)<n: return out
    s=a[0]; out[0]=a[0]
    for i in range(1,len(a)):
        s=(a[i]-s)*2.0/(n+1)+s; out[i]=s
    return out

def rsi(c,n=14):
    c=np.asarray(c,float); d=np.zeros(len(c)); d[1:]=np.diff(c)
    up=np.where(d>0,d,0.0); dn=np.where(d<0,-d,0.0)
    eu=ema(up,n); ed=ema(dn,n)
    rs=np.where(ed>0, eu/ed, 0.0); return 100-100/(1+rs)

def macd_hist(c):
    e12=ema(c,12); e26=ema(c,26); m=e12-e26; s=ema(m,9); return m-s

def atr(h,l,c,n=14):
    h,l,c=np.asarray(h,float),np.asarray(l,float),np.asarray(c,float)
    pc=np.roll(c,1); tr=np.maximum.reduce([h-l, np.abs(h-pc), np.abs(l-pc)])
    return ema(tr,n)

def roc(c,n=10): c=np.asarray(c,float); return np.where(np.roll(c,n)>0,(c/np.roll(c,n)-1)*100, 0.0)

def cci(h,l,c,n=20):
    h,l,c=np.asarray(h,float),np.asarray(l,float),np.asarray(c,float)
    tp=(h+l+c)/3; sma=ema(tp,n); md=ema(np.abs(tp-sma),n); return np.where(md>0,(tp-sma)/(0.015*md),0.0)

def vortex(h,l,c,n=14):
    h,l=np.asarray(h,float),np.asarray(l,float)
    vm_up=np.abs(h[1:]-l[:-1]); vm_dn=np.abs(l[1:]-h[:-1])
    su=ema(vm_up,n); sd=ema(vm_dn,n)
    out=np.full(len(h),np.nan); out[1:]=su/sd
    return out

def vpin_5m(bars1m, B=30):
    b5={}
    for ts,o,h,l,c,v in bars1m:
        b=int(ts)//300*300
        if b not in b5: b5[b]=[o,h,l,c,0.0]
        b5[b][1]=max(b5[b][1],h); b5[b][2]=min(b5[b][2],l); b5[b][3]=c; b5[b][4]+=v
    arr=sorted(b5.items())
    n=len(arr); bv=np.zeros(n); sv=np.zeros(n); vv=np.zeros(n); ts5=np.zeros(n,dtype=int)
    for i,(t,(_,_,_,c,v)) in enumerate(arr):
        ts5[i]=t
        if c>=arr[i][1][0]: bv[i]=v
        else: sv[i]=v
        vv[i]=v
    out=np.full(n,np.nan)
    for i in range(B-1,n):
        bb=bv[i-B+1:i+1].sum(); ss=sv[i-B+1:i+1].sum(); tot=vv[i-B+1:i+1].sum()
        out[i]=(abs(bb-ss))/tot if tot>0 else 0.0
    return dict(zip(ts5,out))

def amihud_5m(bars1m):
    b5={}
    for ts,o,h,l,c,v in bars1m:
        b=int(ts)//300*300
        if b not in b5: b5[b]=[o,h,l,c,0.0]
        b5[b][1]=max(b5[b][1],h); b5[b][2]=min(b5[b][2],l); b5[b][3]=c; b5[b][4]+=v
    out={}
    for t,(o,h,l,c,v) in sorted(b5.items()):
        out[t]=abs((c-o)/o)/v if (v>0 and o>0) else 0.0
    return out

def _wait_h082():
    import time, os
    targets={v:f"/Users/Denis/Dev/Deeptrading/backend/reports/_h082_{v}.json" for v in FIGIS.values()}
    for _ in range(300):
        ok=True
        for p in targets.values():
            try:
                if not (os.path.exists(p) and json.load(open(p)) and "entry_ts" in json.load(open(p))[0]):
                    ok=False; break
            except Exception:
                ok=False; break
        if ok: return
        time.sleep(15)

def main():
    _wait_h082()
    figi=sys.argv[1]; sym=FIGIS[figi]
    ts=[];O=[];H=[];L=[];C=[];V=[]; vpin_map={}; ami_map={}
    for wn,wa,wb in _months():
        b=load_1m(figi,wa,wb)
        if not b: continue
        vmap=vpin_5m(b); amap=amihud_5m(b)
        for t,o,h,l,c,v in b:
            ts.append(t);O.append(o);H.append(h);L.append(l);C.append(c);V.append(v)
        vpin_map.update(vmap); ami_map.update(amap)
    ts=np.array(ts); C=np.array(C);H=np.array(H);L=np.array(L);O=np.array(O);V=np.array(V)
    sig=dict(rsi=rsi(C,14), macd=macd_hist(C), atr=atr(H,L,C,14),
             natr=atr(H,L,C,14)/C*100, roc=roc(C,10), cci=cci(H,L,C,20), vortex=vortex(H,L,C,14))
    tidx={t:i for i,t in enumerate(ts)}
    trades=[]
    for f in glob.glob("/Users/Denis/Dev/Deeptrading/backend/reports/_h082_*.json"):
        for r in json.load(open(f)):
            if r["figi"]==sym: trades.append(r)
    def tsec(tsiso): return int(datetime.fromisoformat(tsiso.replace("Z","+00:00")).timestamp())
    rows=[]
    for r in trades:
        i=tidx.get(tsec(r["entry_ts"]))
        if i is None or i>=len(C): continue
        vp=vpin_map.get(int(tsec(r["entry_ts"]))//300*300, np.nan)
        ami=ami_map.get(int(tsec(r["entry_ts"]))//300*300, np.nan)
        fr=float(r["base_net"])/float(r["base_notional"]) if r["base_notional"]>0 else 0.0
        rec=dict(vpin=vp, ami=ami, future_ret=fr)
        for k,v in sig.items(): rec[k]=float(v[i]) if i<len(v) and not np.isnan(v[i]) else np.nan
        rows.append(rec)
    # VPIN filter
    vps=np.array([x["vpin"] for x in rows]); fr=np.array([x["future_ret"] for x in rows])
    q90=np.nanpercentile(vps,90); m=vps<=q90
    full=fr.mean(); filt=fr[m].mean(); cov=float(m.mean())
    amis=np.array([x["ami"] for x in rows]); valid=~np.isnan(amis)
    corr=float(np.corrcoef(amis[valid], np.abs(fr[valid]))[0,1]) if valid.sum()>2 else None
    vpin_res=dict(figi=figi,n=len(rows),q90_vpin=round(float(q90),3),
                  full_ret=round(float(full),4), filt_ret=round(float(filt),4),
                  coverage=round(cov,3), ratio=round(float(filt/full),3) if full!=0 else None,
                  amihud_absret_corr=round(corr,3) if corr is not None else None,
                  gate_passed=bool(full!=0 and filt/full>=1.15 and cov>=0.5))
    # IC
    try:
        from scipy.stats import spearmanr
        def ic(a,b):
            m=~np.isnan(a)&~np.isnan(b); 
            return spearmanr(a[m],b[m]).correlation if m.sum()>10 else np.nan
    except Exception:
        def ic(a,b):
            m=~np.isnan(a)&~np.isnan(b)
            ra=np.argsort(np.argsort(a[m])); rb=np.argsort(np.argsort(b[m]))
            return float(np.corrcoef(ra,rb)[0,1]) if m.sum()>10 else np.nan
    ic_res={}
    for k in sig:
        s=np.array([x[k] for x in rows])
        val=ic(s,fr); ic_res[k]=round(float(val),3) if val==val else None
    n=len(rows); icir={k: round(v*np.sqrt(n),2) if v is not None else None for k,v in ic_res.items()}
    out=dict(vpin=vpin_res, ic=ic_res, icir=icir, n_total=n)
    json.dump(out, open(f"/Users/Denis/Dev/Deeptrading/backend/reports/_h084_085_{sym}.json","w"), indent=2)
    print(json.dumps(out, indent=2))

if __name__=="__main__": main()
