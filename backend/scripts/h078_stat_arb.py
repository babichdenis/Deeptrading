import sys, json
import numpy as np
sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
from app.services.research_pack import _load_candles
from datetime import datetime, timezone

PAIRS = [("BBG008F2T3T2","BBG004S681B4"), ("BBG004S683W7","BBG004S68CP5")]  # RUAL-NLMK, AFLT-MVID
NAMES = {("BBG008F2T3T2","BBG004S681B4"):"RUAL_NLMK", ("BBG004S683W7","BBG004S68CP5"):"AFLT_MVID"}

def _months():
    for y in (2025,2026):
        for m in range(1,13):
            if (y,m) > (2026,6): break
            if (y,m) < (2025,1): continue
            wa=datetime(y,m,1,tzinfo=timezone.utc)
            wb=datetime(y+1,1,1,tzinfo=timezone.utc) if m==12 else datetime(y,m+1,1,tzinfo=timezone.utc)
            yield f"{y}-{m:02d}", wa, wb

def load_closes(figi, wa, wb):
    return [(int(c.ts.timestamp()), float(c.close)) for c in _load_candles(figi, wa, wb)]

def ols_beta(x, y):
    xm=x-x.mean(); ym=y-y.mean()
    return float((xm*ym).sum()/(xm*xm).sum())

def adf(series, maxlag=1):
    s=np.asarray(series,float); d=np.diff(s); n=len(d)
    y=d[maxlag:]; X=[np.ones(n-maxlag), s[:-1][maxlag:]]
    for l in range(1,maxlag): X.append(d[maxlag-l-1:-1-l])
    X=np.array(X).T
    beta,_,_,_=np.linalg.lstsq(X,y,rcond=None); resid=y-X@beta
    dof=n-maxlag-len(beta); sigma2=(resid@resid)/dof; se=np.sqrt(sigma2*np.linalg.inv(X.T@X)[1,1])
    tstat=beta[1]/se
    from math import erf, sqrt
    p=2*(1-0.5*(1+erf(abs(tstat)/sqrt(2))))
    return float(tstat), float(p)

def pairs_sim(spread):
    z=(spread-spread.mean())/spread.std(); pos=0; entry=0; trades=[]
    for i in range(len(z)):
        if pos==0 and z[i]<-2: pos=1; entry=i
        elif pos==0 and z[i]>2: pos=-1; entry=i
        elif pos!=0 and (abs(z[i])<0.5 or i-entry>=20):
            trades.append(pos*(spread[i]-spread[entry])); pos=0
    if pos!=0: trades.append(pos*(spread[-1]-spread[entry]))
    return np.array(trades)

def main():
    out={}
    for (f1,f2) in PAIRS:
        closes={}
        for figi in (f1,f2):
            closes[figi]={}
            for wn,wa,wb in _months():
                for t,c in load_closes(figi,wa,wb): closes[figi][t]=c
        ts=sorted(set(closes[f1])&set(closes[f2]))
        p1=np.array([closes[f1][t] for t in ts]); p2=np.array([closes[f2][t] for t in ts])
        beta=ols_beta(p2,p1); spread=p1-beta*p2
        tstat,p=adf(spread,1)
        tr=pairs_sim(spread); net=float(tr.sum()); win=float((tr>0).mean()) if len(tr)>0 else 0.0
        rng=np.random.default_rng(42); perm=[]
        for _ in range(200):
            sp=rng.permutation(spread); perm.append(pairs_sim(sp).sum())
        pval=float(np.mean([1 if pn>=net else 0 for pn in perm]))
        out[NAMES[(f1,f2)]]=dict(beta=round(beta,4), adf_tstat=round(tstat,2), adf_p=round(p,4),
                                  stationary=bool(p<0.05), n_trades=len(tr), net=round(net,2),
                                  win=round(win,3), h058_pvalue=round(pval,3))
    json.dump(out, open("/Users/Denis/Dev/Deeptrading/backend/reports/h078_stat_arb.json","w"), indent=2)
    print(json.dumps(out, indent=2))

if __name__=="__main__": main()
