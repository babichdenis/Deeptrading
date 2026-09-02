import sys, json, glob
import numpy as np

CAP = 10000.0
FILES = sorted(glob.glob("/Users/Denis/Dev/Deeptrading/backend/reports/_h082_*.json"))
data = {}
for f in FILES:
    for r in json.load(open(f)):
        figi = r["figi"]; d = r["exit_ts"][:10]
        net = float(r.get("base_net", r.get("net", 0.0)))
        data.setdefault(figi, {})
        data[figi][d] = data[figi].get(d, 0.0) + net
figis = sorted(data)
dates = sorted({d for fi in figis for d in data[fi]})
M = np.array([[data[fi].get(d, 0.0) for d in dates] for fi in figis])  # (5, T) daily net
R = M / CAP  # daily return per ticker (each funded with CAP)
print("figis", figis, "dates", dates[0], "->", dates[-1], "T", len(dates))

def hrp_weights(cov):
    try:
        from scipy.cluster.hierarchy import linkage, to_tree
        from scipy.spatial.distance import squareform
        d = np.sqrt(np.diag(cov)); corr = np.clip(cov/np.outer(d,d), -1, 1)
        dist = np.sqrt(0.5*(1-corr))
        Z = linkage(squareform(dist, checks=False), method="ward")
        tree = to_tree(Z, rd=False)
        def _leaves(n): return [n.id] if n.is_leaf() else _leaves(n.left)+_leaves(n.right)
        order = _leaves(tree)
    except Exception as e:
        order = list(range(len(cov)))
    iv = 1.0/np.diag(cov); w = np.zeros(len(cov))
    def _bisect(idx):
        if len(idx)==1: w[idx[0]]=1.0; return
        m=len(idx)//2; L,Rr=idx[:m],idx[m:]
        def var(c): s=cov[np.ix_(c,c)]; x=np.ones(len(c))/len(c); return x@s@x
        vl,vr=var(L),var(Rr); a=(1/vl)/(1/vl+1/vr)
        _bisect(L); _bisect(Rr); w[L]*=a; w[Rr]*=(1-a)
    _bisect(order); return w/w.sum()

def metrics(pr):
    eq = np.cumprod(1+pr); ret = eq[-1]-1
    gp = pr[pr>0].sum(); gl = -pr[pr<0].sum()
    pf = float(gp/gl) if gl>0 else float("inf")
    peak = np.maximum.accumulate(eq); dd = float((eq-peak).min())
    return dict(net_pct=round(ret,4), net_abs=round(float(pr.sum())*CAP*len(figis),1),
                pf=round(pf,3), max_dd=round(dd,4))

def wf(train_mask, test_mask):
    cov = np.cov(R[:, train_mask])
    if np.any(np.isnan(cov)) or cov.shape!=(len(figis),len(figis)):
        cov = np.eye(len(figis))
    w = hrp_weights(cov)
    pr_hrp = w @ R[:, test_mask]
    pr_eq = (np.ones(len(figis))/len(figis)) @ R[:, test_mask]
    pr_cap = R[:, test_mask].sum(axis=0)  # all tickers equal CAP each (cap25-approx)
    return w, pr_hrp, pr_eq, pr_cap

windows = [("2025-07","2025-H2"),("2026-01","2026-H1")]
allp = {"hrp":[], "eq":[], "cap":[]}; herf=[]; details=[]
for start, label in windows:
    si = dates.index(start+"-01") if start+"-01" in dates else 0
    tm = np.array([d>=start for d in dates])
    trm = np.arange(si, len(dates))  # train = all preceding
    w, ph, pe, pc = wf(trm, tm)
    allp["hrp"].append(ph); allp["eq"].append(pe); allp["cap"].append(pc)
    herf.append(float((w**2).sum()))
    details.append(dict(window=label, weights=dict(zip(figis, [round(float(x),3) for x in w])), herfindahl=round(float((w**2).sum()),3)))
agg = {k: metrics(np.concatenate(v)) for k,v in allp.items()}
agg["herfindahl_hrp"] = round(float(np.mean(herf)),3)
agg["herfindahl_eq"] = round(1.0/len(figis),3)
out = dict(n_figi=len(figis), windows=[w[0] for w in windows],
           hrp=agg["hrp"], equal=agg["eq"], cap25_approx=agg["cap"],
           herfindahl=dict(hrp=agg["herfindahl_hrp"], equal=agg["herfindahl_eq"]),
           details=details)
json.dump(out, open("/Users/Denis/Dev/Deeptrading/backend/reports/h083_hrp.json","w"), indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2, ensure_ascii=False))
