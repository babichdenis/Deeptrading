# -*- coding: utf-8 -*-
"""Validate entry-scan findings: macro diagnosis, monthly IC stability, composite gates train->test."""
import numpy as np
import pandas as pd

np.seterr(divide="ignore", invalid="ignore")
DIR = "X:/Deeptrading/backend/reports/feature_scan"
fd = pd.read_csv(DIR + "/feature_matrix.csv")
fd["entry_time"] = pd.to_datetime(fd["entry_time"], utc=True)
fd = fd.sort_values("entry_time").reset_index(drop=True)
print("rows %d span %s .. %s" % (len(fd), fd.entry_time.min(), fd.entry_time.max()))

print("== MACRO DIAG ==")
for c in ("usd_r1", "usd_r5", "eur_r5", "im_r20", "cny_r1", "gold_r5", "brent_r5", "ng_r1"):
    if c not in fd.columns:
        print("DIAG %-9s MISSING" % c)
        continue
    x = pd.to_numeric(fd[c], errors="coerce").values.astype(float)
    fin = x[np.isfinite(x)]
    u = pd.unique(fin)
    print("DIAG %-9s finite=%4d uniq=%3d sample=%s" % (c, len(fin), len(u), np.round(np.sort(u)[:5], 6)))

cut = int(len(fd) * 0.7)
tr, te = fd.iloc[:cut].copy(), fd.iloc[cut:].copy()
for tag, g in (("TRAIN", tr), ("TEST ", te)):
    y = g.net_pnl.values.astype(float)
    print("%s n=%4d wr=%.3f mean=%+.2f tot=%+.0f" % (tag, len(g), (y > 0).mean(), y.mean(), y.sum()))


def spear(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.std(x[ok]) == 0:
        return np.nan
    return float(np.corrcoef(pd.Series(x[ok]).rank().values, pd.Series(y[ok]).rank().values)[0, 1])


fd["ym"] = fd.entry_time.dt.tz_convert("Europe/Moscow").dt.strftime("%Y-%m")
SETS = {
    "BUY": ["h_rsi", "h_dema", "h_ret6", "h_ret1", "h_pos24", "m5_day"],
    "SELL": ["h_rsi", "m10_rsi", "h_dema", "z_h_ret3", "f_dow", "z_m5_ret12"],
    "ALL": ["m10_ret12", "f_hm", "f_hour", "h_ret1", "z_h_ret6"],
}
for side, feats in SETS.items():
    g = fd if side == "ALL" else fd[fd.side == side]
    print("== monthly rank-IC %s ==" % side)
    rows = []
    for ym, gg in g.groupby("ym"):
        y = gg.net_pnl.values.astype(float)
        row = {"ym": ym, "n": len(gg)}
        for f in feats:
            v = spear(pd.to_numeric(gg[f], errors="coerce").values.astype(float), y)
            row[f] = round(v, 2) if np.isfinite(v) else np.nan
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))


def gate_masks(s):
    d = s["dir"]
    mom = (d * pd.to_numeric(s["h_ret6"], errors="coerce")) > 0
    rsi = (d * (pd.to_numeric(s["h_rsi"], errors="coerce") - 50.0)) > 0
    nochase = (d * pd.to_numeric(s["h_ret1"], errors="coerce")) < 0.004
    early = s["f_hm"] <= 12.0
    dow4 = s["f_dow"] >= 4
    return {"mom6": mom, "rsi50": rsi, "nochase": nochase, "early": early, "dow4": dow4}


def show(s, mask, name):
    m = mask.values if hasattr(mask, "values") else np.asarray(mask)
    x = s[m]
    y = x.net_pnl.values.astype(float)
    if len(y) == 0:
        print("  %-26s n=   0" % name)
        return
    print("  %-26s n=%4d wr=%.3f mean=%+6.2f tot=%+7.0f" % (name, len(y), (y > 0).mean(), y.mean(), y.sum()))


for side in ("BUY", "SELL"):
    for tag, frame in (("train", tr[tr.side == side]), ("test", te[te.side == side])):
        gm = gate_masks(frame)
        y = frame.net_pnl.values.astype(float)
        print("== %s %s: n=%d wr=%.3f mean=%+.2f ==" % (side, tag, len(frame), (y > 0).mean(), y.mean()))
        show(frame, pd.Series(True, index=frame.index), "baseline(all)")
        for k in ("mom6", "rsi50", "nochase", "early", "dow4"):
            if k == "dow4" and side != "SELL":
                continue
            show(frame, gm[k], k)
        show(frame, gm["mom6"] & gm["rsi50"], "mom6 & rsi50")
        show(frame, gm["mom6"] & gm["rsi50"] & gm["nochase"], "mom6 & rsi50 & nochase")
        show(frame, ~(gm["mom6"] & gm["rsi50"]), "complement(mom6&rsi50)")

print("== TEST combined portfolio ==")
keep = pd.Series(False, index=te.index)
for side in ("BUY", "SELL"):
    sub = te[te.side == side]
    gm = gate_masks(sub)
    m = gm["mom6"] & gm["rsi50"]
    keep.loc[m.index] = m.values
show(te, keep, "gated portfolio")
show(te, ~keep, "skipped by gates")
y = te.net_pnl.values.astype(float)
print("test total pnl baseline %+.0f -> gated %+.0f" % (y.sum(), te[keep.values].net_pnl.sum()))
print("DONE")
