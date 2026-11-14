# -*- coding: utf-8 -*-
"""Diagnose constant-macro bug; rebuild macro features (strict prior day) into feature_matrix2.csv."""
import numpy as np
import pandas as pd
import psycopg2

np.seterr(divide="ignore", invalid="ignore")
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NSD = 86_400_000_000_000
H3 = 3 * 3_600_000_000_000
EPO = 719163
DIR = "X:/Deeptrading/backend/reports/feature_scan"

conn = psycopg2.connect(DSN)
mac = pd.read_sql("SELECT secid, ts, close FROM macro_daily", conn)
print("dtypes: ts=%s close=%s" % (mac["ts"].dtype, mac["close"].dtype))
mac["close"] = mac["close"].astype(float)
print(mac.groupby("secid")["close"].agg(["count", "nunique", "min", "max"]).round(4).to_string())
mac["ord"] = pd.DatetimeIndex(pd.to_datetime(mac["ts"].astype(str).str[:10])).asi8 // NSD + EPO
print(mac[["secid", "ts", "close", "ord"]].head(3).to_string())
print(mac.groupby("secid")["ord"].agg(["min", "max", "nunique"]).to_string())

MAC = {}
for s, g in mac.groupby("secid"):
    g = g.sort_values("ord").drop_duplicates("ord", keep="last")
    c = pd.Series(g["close"].values)
    MAC[s] = dict(ord=g["ord"].values, r1=c.pct_change().values,
                  r5=(c / c.shift(5) - 1).values, r20=(c / c.shift(20) - 1).values)

fd = pd.read_csv(DIR + "/feature_matrix.csv")
fd["entry_time"] = pd.to_datetime(fd["entry_time"], utc=True)
fd = fd.sort_values("entry_time").reset_index(drop=True)
e_ns = pd.DatetimeIndex(fd["entry_time"]).asi8
e_ord = (e_ns + H3) // NSD + EPO
dstr = fd.entry_time.dt.tz_convert("Europe/Moscow").dt.strftime("%Y-%m-%d")
e_ord2 = pd.DatetimeIndex(pd.to_datetime(dstr)).asi8 // NSD + EPO
print("e_ord  sample %s range %d..%d" % (e_ord[:4], e_ord.min(), e_ord.max()))
print("e_ord2 sample %s range %d..%d" % (e_ord2[:4], e_ord2.min(), e_ord2.max()))
print("diff e_ord-e_ord2 uniq:", np.unique(e_ord - e_ord2))
for s in ("USDRUB", "IMOEX", "BRENT"):
    M = MAC[s]
    q = np.searchsorted(M["ord"], e_ord2, side="left") - 1
    print("%-7s ord %d..%d n=%d | q uniq=%d [%d..%d]" %
          (s, M["ord"].min(), M["ord"].max(), len(M["ord"]), len(np.unique(q)), q.min(), q.max()))

print("== CSV diag: daily cols (n=%d) ==" % len(fd))
for c in ("d_ret1", "d_ret5", "d_pos20", "d_atr", "d_rsi", "d_slope20", "d_vol20"):
    x = pd.to_numeric(fd[c], errors="coerce").values.astype(float)
    fin = x[np.isfinite(x)]
    print("CSV %-10s finite=%4d uniq=%4d" % (c, len(fin), len(pd.unique(fin))))

MKEYS = {"IMOEX": "im", "USDRUB": "usd", "EURRUB": "eur", "CNYRUB_TOM": "cny",
         "GLDRUB_TOM": "gold", "BRENT": "brent", "NG": "ng"}
for s, pre in MKEYS.items():
    M = MAC[s]
    q = np.searchsorted(M["ord"], e_ord2, side="left") - 1
    same = (q >= 0) & (M["ord"][np.clip(q, 0, None)] == e_ord2)
    q = np.where(same, q - 1, q)
    valid = q >= 0
    qc = np.clip(q, 0, None)
    fd[pre + "_r1"] = np.where(valid, M["r1"][qc], np.nan)
    fd[pre + "_r5"] = np.where(valid, M["r5"][qc], np.nan)
    if s == "IMOEX":
        fd["im_r20"] = np.where(valid, M["r20"][qc], np.nan)
fd.to_csv(DIR + "/feature_matrix2.csv", index=False)
print("== rebuilt macro (strict prior day, no lookahead) ==")
for c in ("usd_r1", "usd_r5", "eur_r5", "im_r20", "cny_r1", "gold_r5", "brent_r5", "ng_r1"):
    x = pd.to_numeric(fd[c], errors="coerce").values.astype(float)
    fin = x[np.isfinite(x)]
    print("NEW %-9s finite=%4d uniq=%4d mean=%+.5f" % (c, len(fin), len(pd.unique(fin)),
                                                       fin.mean() if len(fin) else 0.0))

fd["ym"] = fd.entry_time.dt.tz_convert("Europe/Moscow").dt.strftime("%Y-%m")

def spear(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.std(x[ok]) == 0:
        return np.nan
    return float(np.corrcoef(pd.Series(x[ok]).rank().values,
                             pd.Series(y[ok]).rank().values)[0, 1])

MCO = sorted({p + k for p in ("im_", "usd_", "eur_", "cny_", "gold_", "brent_", "ng_")
              for k in ("r1", "r5")} | {"im_r20"})
MCO = [c for c in MCO if c in fd.columns]
for side, g in (("ALL", fd), ("BUY", fd[fd.side == "BUY"]), ("SELL", fd[fd.side == "SELL"])):
    y = g.net_pnl.values.astype(float)
    w = (y > 0).astype(float)
    print("== %s n=%d wr=%.3f macro terciles ==" % (side, len(g), w.mean()))
    rows = []
    for c in MCO:
        x = pd.to_numeric(g[c], errors="coerce").values.astype(float)
        okm = np.isfinite(x)
        ic = spear(x, y)
        rk = pd.Series(x[okm]).rank(method="first").values
        ql = pd.qcut(rk, 3, labels=False)
        top, bot = ql == 2, ql == 0
        rows.append((c, int(okm.sum()), round(ic, 3) if np.isfinite(ic) else np.nan,
                     round(w[top].mean() - w[bot].mean(), 3),
                     round(y[top].mean() - y[bot].mean(), 1)))
    res = pd.DataFrame(rows, columns=["feature", "n", "ic", "wrS", "pnlS"])
    res = res.iloc[np.argsort(-res.ic.abs().fillna(0).values)]
    for _, rr in res.iterrows():
        print("%-10s n=%4d ic=%s wrS=%+.3f pnlS=%+7.1f" %
              (rr.feature, rr.n, ("%+.3f" % rr.ic) if np.isfinite(rr.ic) else "  nan",
               rr.wrS, rr.pnlS))

CAND = ["usd_r5", "im_r20", "brent_r5", "ng_r1", "gold_r5", "cny_r1"]
for side in ("BUY", "SELL"):
    g = fd[fd.side == side]
    print("== monthly macro IC %s ==" % side)
    rows = []
    for ym, gg in g.groupby("ym"):
        y = gg.net_pnl.values.astype(float)
        row = {"ym": ym, "n": len(gg)}
        for c in CAND:
            v = spear(pd.to_numeric(gg[c], errors="coerce").values.astype(float), y)
            row[c] = round(v, 2) if np.isfinite(v) else np.nan
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))
print("DONE")
