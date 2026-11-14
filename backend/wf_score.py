# -*- coding: utf-8 -*-
"""Walk-forward composite score + daily x H1 combos + feature sign stability."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

DIR = "X:/Deeptrading/backend/reports/feature_scan"
fd = pd.read_csv(DIR + "/feature_matrix2.csv")
fd["entry_time"] = pd.to_datetime(fd["entry_time"], utc=True)
fd["ym"] = fd["entry_time"].dt.strftime("%Y-%m")
fd["net_pnl"] = fd["net_pnl"].astype(float)
fd["half"] = np.where(fd["ym"] <= "2026-06", "IS", "OOS")

DAILY = ["d_rsi","d_ret1","d_ret5","d_ret20","d_slope20","d_atr","d_vol20",
         "d_pos20","d_above20","d_above50","d_rng","d_body"]
MACRO = ["im_r1","im_r5","im_r20","usd_r1","usd_r5","eur_r1","eur_r5","cny_r1",
         "cny_r5","gold_r1","gold_r5","brent_r1","brent_r5","ng_r1","ng_r5"]
INTRADAY = ["h_ret1","h_ret3","h_ret6","h_rsi","h_vrel","h_dema","h_pos24",
            "m10_ret3","m10_ret12","m10_rsi","m5_ret3","m5_ret12","m5_rsi"]
FEATS = DAILY + MACRO + INTRADAY

for c in FEATS:
    fd[c] = pd.to_numeric(fd[c], errors="coerce")

def ic_spear(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.std(x[ok]) == 0:
        return 0.0
    return float(np.corrcoef(pd.Series(x[ok]).rank().values,
                             pd.Series(y[ok]).rank().values)[0, 1])

months = sorted(fd["ym"].unique())
print("months:", months)
wf_parts = []
for side in ("BUY", "SELL"):
    g = fd[fd.side == side]
    for m in months:
        hist = g[g["ym"] < m]
        cur = g[g["ym"] == m]
        if len(cur) == 0 or len(hist) < 60:
            continue
        signs = {}
        for c in FEATS:
            s = np.sign(ic_spear(hist[c].values, hist["net_pnl"].values))
            if s != 0:
                signs[c] = int(s)
        if not signs:
            continue
        R = []
        for c in signs:
            R.append(signs[c] * cur[c].rank(pct=True).values)
        S = np.nan_to_num(np.nanmean(np.vstack(R), axis=0), nan=0.5)
        tmp = pd.DataFrame({"ym": m, "half": cur["half"].values,
                            "net_pnl": cur["net_pnl"].values, "score": S,
                            "side": side, "n_sig": len(signs)})
        if len(tmp) >= 12:
            q = pd.qcut(pd.Series(S).rank(method="first"), 3, labels=False)
        else:
            q = pd.Series(1, index=tmp.index)
        tmp["terc"] = q.values
        wf_parts.append(tmp)
wf = pd.concat(wf_parts)

for side in ("BUY", "SELL"):
    w = wf[wf.side == side]
    print("== WF %s (signs from prior months only) ==" % side)
    for t, lab in ((2, "T3"), (1, "T2"), (0, "T1")):
        k = w[w.terc == t]
        kis = k[k.half == "IS"]; koo = k[k.half == "OOS"]
        print("  %s n=%4d avg=%+7.2f wr=%.2f sum=%+8.0f | IS avg=%+7.2f | OOS avg=%+7.2f wr=%.2f" %
              (lab, len(k), k.net_pnl.mean(), (k.net_pnl > 0).mean(), k.net_pnl.sum(),
               kis.net_pnl.mean() if len(kis) else np.nan,
               koo.net_pnl.mean() if len(koo) else np.nan,
               (koo.net_pnl > 0).mean() if len(koo) else np.nan))
    per = w.groupby(["ym", "terc"])["net_pnl"].agg(["mean", "count"]).unstack()
    print("  monthly T3 vs T1 avg:")
    for m in sorted(w.ym.unique()):
        try:
            a3 = per.loc[(m, ("mean", 2))]; a1 = per.loc[(m, ("mean", 0))]
            print("    %s T3=%+7.2f (n=%3d)  T1=%+7.2f (n=%3d)" % (m, a3, per.loc[(m,("count",2))], a1, per.loc[(m,("count",0))]))
        except KeyError:
            pass

print("== sign stability IS vs OOS (|ic|>=.05 both, same sign) ==")
for side in ("BUY", "SELL"):
    g = fd[fd.side == side]
    rows = []
    for c in FEATS:
        i = ic_spear(g[g.half == "IS"][c].values, g[g.half == "IS"]["net_pnl"].values)
        o = ic_spear(g[g.half == "OOS"][c].values, g[g.half == "OOS"]["net_pnl"].values)
        rows.append((c, i, o, (np.sign(i) == np.sign(o)) and abs(i) >= .05 and abs(o) >= .05))
    rb = pd.DataFrame(rows, columns=["f", "icIS", "icOOS", "stable"])
    st = rb[rb.stable].sort_values("icIS", key=lambda s: -s.abs())
    print("%s stable:" % side, ", ".join("%s(%+.2f/%+.2f)" % (r.f, r.icIS, r.icOOS) for r in st.itertuples()))

print("== daily x H1 combos ==")
def prep(side):
    g = fd[fd.side == side].copy()
    thr = {}
    for c in ("d_atr", "usd_r5", "d_rsi"):
        thr[c] = float(g.loc[g.half == "IS", c].median())
        g[c] = g[c].fillna(thr[c])
    return g, thr

def report(name, g, mask):
    k = g[mask]; d = g[~mask]
    def st(x): return (len(x), x.net_pnl.mean(), (x.net_pnl > 0).mean(), x.net_pnl.sum())
    ki = st(k[k.half == "IS"]); ko = st(k[k.half == "OOS"])
    di = st(d[d.half == "IS"]); do = st(d[d.half == "OOS"])
    print("%-38s | keep IS n=%4d avg=%+7.2f | keep OOS n=%4d avg=%+7.2f wr=%.2f | drop IS=%+7.2f OOS=%+7.2f" %
          (name, ki[0], ki[1], ko[0], ko[1], ko[2], di[1], do[1]))

BUY_C = [
    ("d_rsi<=40", lambda g,t: g.d_rsi <= 40),
    ("d_rsi<=40 & h_ret3>0", lambda g,t: (g.d_rsi <= 40) & (g.h_ret3 > 0)),
    ("d_ret5<=0 & h_ret3>0", lambda g,t: (g.d_ret5 <= 0) & (g.h_ret3 > 0)),
    ("d_rsi<=45 & atr>=med & h_ret3>0", lambda g,t: (g.d_rsi <= 45) & (g.d_atr >= t["d_atr"]) & (g.h_ret3 > 0)),
    ("d_pos20<=.35 & h_pos24<=.5", lambda g,t: (g.d_pos20 <= .35) & (g.h_pos24 <= .5)),
    ("h_rsi<=35 & d_ret5<=0", lambda g,t: (g.h_rsi <= 35) & (g.d_ret5 <= 0)),
]
SELL_C = [
    ("h_ret3<0", lambda g,t: g.h_ret3 < 0),
    ("d_atr>=med & h_ret3<0", lambda g,t: (g.d_atr >= t["d_atr"]) & (g.h_ret3 < 0)),
    ("usd_r5>=med & h_ret3<0", lambda g,t: (g.usd_r5 >= t["usd_r5"]) & (g.h_ret3 < 0)),
    ("d_rsi>=60 & h_ret3<0", lambda g,t: (g.d_rsi >= 60) & (g.h_ret3 < 0)),
    ("im_r1<=0 & h_ret3<0", lambda g,t: (g.im_r1 <= 0) & (g.h_ret3 < 0)),
]
for side, rules in (("BUY", BUY_C), ("SELL", SELL_C)):
    g, thr = prep(side)
    print("-- %s n=%d --" % (side, len(g)))
    for name, fn in rules:
        m = fn(g, thr).fillna(False).values.astype(bool)
        report(name, g, m)
print("DONE")
