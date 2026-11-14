# -*- coding: utf-8 -*-
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

DIR = "X:/Deeptrading/backend/reports/feature_scan"
fd = pd.read_csv(DIR + "/feature_matrix2.csv")
fd["entry_time"] = pd.to_datetime(fd["entry_time"], utc=True)
fd["ym"] = fd["entry_time"].dt.strftime("%Y-%m")
fd["half"] = np.where(fd["ym"] <= "2026-06", "IS", "OOS")
fd["net_pnl"] = fd["net_pnl"].astype(float)

def stat(g):
    return (len(g), g["net_pnl"].mean(), (g["net_pnl"] > 0).mean(), g["net_pnl"].sum())

print("BASE IS  n=%4d avg=%+.2f wr=%.3f sum=%+.0f" % stat(fd[fd.half == "IS"]))
print("BASE OOS n=%4d avg=%+.2f wr=%.3f sum=%+.0f" % stat(fd[fd.half == "OOS"]))

FEATS_USE = ["d_rsi", "d_ret5", "d_ret20", "d_slope20", "d_atr", "d_vol20", "d_pos20",
             "usd_r1", "usd_r5", "eur_r5", "cny_r5", "brent_r1", "brent_r5",
             "im_r1", "im_r5", "im_r20", "gold_r5", "ng_r1"]

def prep(side):
    g = fd[fd.side == side].copy()
    thr = {}
    for c in FEATS_USE:
        v = pd.to_numeric(g.loc[g.half == "IS", c], errors="coerce")
        thr[c] = float(v.median())
        g[c] = pd.to_numeric(g[c], errors="coerce").fillna(thr[c])
    return g, thr

def report(name, g, mask):
    k = g[mask]; d = g[~mask]
    i = stat(k[k.half == "IS"]); o = stat(k[k.half == "OOS"])
    di = stat(d[d.half == "IS"]); do = stat(d[d.half == "OOS"])
    print("%-26s | IS k n=%4d avg=%+7.2f wr=%.2f | OOS k n=%4d avg=%+7.2f wr=%.2f | dropAvg IS=%+7.2f OOS=%+7.2f" %
          (name, i[0], i[1], i[2], o[0], o[1], o[2], di[1], do[1]))

BUY_RULES = [
    ("rsi<=40",          lambda g, t: g["d_rsi"] <= 40),
    ("ret5<=0",          lambda g, t: g["d_ret5"] <= 0),
    ("slope20<=0",       lambda g, t: g["d_slope20"] <= 0),
    ("atr>=med",         lambda g, t: g["d_atr"] >= t["d_atr"]),
    ("pos20<=0.35",      lambda g, t: g["d_pos20"] <= 0.35),
    ("ret5<=0 & atr>=med", lambda g, t: (g["d_ret5"] <= 0) & (g["d_atr"] >= t["d_atr"])),
    ("rsi<=45 & atr>=med", lambda g, t: (g["d_rsi"] <= 45) & (g["d_atr"] >= t["d_atr"])),
    ("im_r5<=0",         lambda g, t: g["im_r5"] <= 0),
]
SELL_RULES = [
    ("usd_r5>=med",      lambda g, t: g["usd_r5"] >= t["usd_r5"]),
    ("brent_r5>=med",    lambda g, t: g["brent_r5"] >= t["brent_r5"]),
    ("cny_r5>=med",      lambda g, t: g["cny_r5"] >= t["cny_r5"]),
    ("im_r1<=0",         lambda g, t: g["im_r1"] <= 0),
    ("gold_r5>=med",     lambda g, t: g["gold_r5"] >= t["gold_r5"]),
    ("atr>=med",         lambda g, t: g["d_atr"] >= t["d_atr"]),
    ("usd_r5>=med & im_r1<=0", lambda g, t: (g["usd_r5"] >= t["usd_r5"]) & (g["im_r1"] <= 0)),
    ("usd_r5>=med & brent_r5>=med", lambda g, t: (g["usd_r5"] >= t["usd_r5"]) & (g["brent_r5"] >= t["brent_r5"])),
]

for side, rules in (("BUY", BUY_RULES), ("SELL", SELL_RULES)):
    g, thr = prep(side)
    print("== %s n=%d (IS %d / OOS %d) ==" % (side, len(g), (g.half == "IS").sum(), (g.half == "OOS").sum()))
    for name, fn in rules:
        try:
            m = fn(g, thr).fillna(False).values.astype(bool)
        except Exception as e:
            print(name, "ERR", e); continue
        report(name, g, m)
    gi = g[g.half == "IS"]

def ic_spear(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.std(x[ok]) == 0:
        return 0.0
    return float(np.corrcoef(pd.Series(x[ok]).rank().values, pd.Series(y[ok]).rank().values)[0, 1])

print("== composite score (signs from IS, applied to OOS) ==")
for side in ("BUY", "SELL"):
    g, thr = prep(side)
    gi = g[g.half == "IS"]
    signs = {}
    for c in FEATS_USE:
        s = np.sign(ic_spear(gi[c].values, gi["net_pnl"].values))
        if s != 0:
            signs[c] = s
    S = np.zeros(len(g))
    for c, s in signs.items():
        S += s * pd.Series(g[c].values).rank(pct=True).values
    g["score"] = S / len(signs)
    print("%s signs: %s" % (side, {c: int(v) for c, v in signs.items()}))
    ql = pd.qcut(pd.Series(g["score"]).rank(method="first"), 3, labels=False)
    report("score T3 (best 3rd)", g, (ql == 2).values)
    report("score T2", g, (ql == 1).values)
    report("score T1 (worst 3rd)", g, (ql == 0).values)
    for h in ("IS", "OOS"):
        gh = g[g.half == h]
        qh = pd.qcut(pd.Series(gh["score"]).rank(method="first"), 3, labels=False)
        for lab, mm in (("T3", qh == 2), ("T2", qh == 1), ("T1", qh == 0)):
            s = stat(gh[mm])
            print("  %s %s-score %s: n=%4d avg=%+7.2f wr=%.2f sum=%+.0f" % (side, h, lab, s[0], s[1], s[2], s[3]))
print("DONE")
