# -*- coding: utf-8 -*-
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

DIR = "X:/Deeptrading/backend/reports/feature_scan"
tr = pd.read_csv(DIR + "/exit_sim3.csv")
tr["entry_time"] = pd.to_datetime(tr["entry_time"], utc=True)
tr["net_pnl"] = pd.to_numeric(tr["net_pnl"], errors="coerce")
tr["mfeR"] = pd.to_numeric(tr["mfeR"], errors="coerce")
tr["maeR"] = pd.to_numeric(tr["maeR"], errors="coerce")
tr["t1R"] = pd.to_numeric(tr["t1R"], errors="coerce")
tr["realR"] = pd.to_numeric(tr["realR"], errors="coerce")
tr["qty"] = pd.to_numeric(tr["qty"], errors="coerce").abs()

POL = ["LASTC", "SL1", "TP05", "TP1", "TP15", "TP2", "TP3", "TP1_SL1", "TP2_SL1",
       "TRAIL_1R05", "TRAIL_15R075", "TRAIL_2R1", "TIME12", "TIME24", "TIME48",
       "BE1R", "TP2_BE_SL1"]
print("== policy deltas vs actual, rub/trade (risk=2%% of entry) ==")
for p in POL:
    d = pd.to_numeric(tr["d_" + p], errors="coerce")
    ok = np.isfinite(d)
    if not ok.any():
        continue
    new = tr.net_pnl.where(~ok, tr.net_pnl[ok] + d[ok])
    print("%-12s ok=%4d meanD=%+7.2f sumD=%+9.0f | BUY %+.2f SELL %+.2f | IS %+.2f OOS %+.2f | wr %.3f->%.3f | newAvg %+.2f" %
          (p, ok.sum(), d[ok].mean(), d[ok].sum(),
           d[ok & (tr.side == "BUY")].mean(), d[ok & (tr.side == "SELL")].mean(),
           d[ok & (tr.half == "IS")].mean(), d[ok & (tr.half == "OOS")].mean(),
           (tr.net_pnl[ok] > 0).mean(), (new[ok] > 0).mean(), new[ok].mean()))

print("== detail: new pnl by side x half ==")
for p in ("SL1", "TRAIL_15R075", "TP2_BE_SL1", "BE1R", "TIME24", "TP2_SL1"):
    for s in ("BUY", "SELL"):
        row = []
        for h in ("IS", "OOS"):
            m = np.isfinite(tr["d_" + p]) & (tr.side == s) & (tr.half == h)
            new = tr.net_pnl[m] + tr["d_" + p][m]
            row.append("%s n=%4d avg=%+7.2f sum=%+8.0f wr=%.2f" % (h, m.sum(), new.mean(), new.sum(), (new > 0).mean()))
        print("%-12s %s | %s | %s" % (p, s, row[0], row[1]))

print("== SL1 stop-hit anatomy (adv>=1R, R=2%%) ==")
rr = tr[np.isfinite(tr.mfeR)].copy()
rr["hit"] = rr.maeR >= 1.0
for s in ("BUY", "SELL"):
    g = rr[rr.side == s]
    h_ = g[g.hit]
    print("%s: hit share=%.2f n=%d | hit avg net=%+7.2f (becomes -1R) | nonhit avg net=%+7.2f" %
          (s, len(h_) / max(len(g), 1), len(h_), h_.net_pnl.mean(), g[~g.hit].net_pnl.mean()))

print("== MFE/MAE true-R (R=2%% of entry) ==")
rr["win"] = rr.net_pnl > 0
print(rr.groupby(["side", "win"])[["mfeR", "maeR", "realR", "t1R"]].mean().round(2).to_string())
for s in ("BUY", "SELL"):
    g = rr[rr.side == s]
    print("%s: MFE>=1R %.2f | >=2R %.2f | >=2R&real<1R %.2f | med t1R=%.1fh | giveback=%.2f" %
          (s, (g.mfeR >= 1).mean(), (g.mfeR >= 2).mean(), ((g.mfeR >= 2) & (g.realR < 1)).mean(),
           g.t1R.median(), (g.mfeR - g.realR.clip(lower=0)).mean()))
print("DONE")
