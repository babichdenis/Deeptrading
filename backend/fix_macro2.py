# -*- coding: utf-8 -*-
"""v2: guarded diag, robust daily-interval pick, NaN-aligned terciles."""
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import psycopg2

np.seterr(divide="ignore", invalid="ignore")
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NSD = 86_400_000_000_000
EPO = 719163
DIR = "X:/Deeptrading/backend/reports/feature_scan"


def day_ord_naive(col):
    s = pd.Series(col).astype(str).str[:10]
    return pd.DatetimeIndex(pd.to_datetime(s)).as_unit("ns").asi8 // NSD + EPO


def day_ord_tz(col):
    s = pd.Series(col)
    try:
        tz = s.dt.tz
    except (AttributeError, TypeError):
        tz = None
    if tz is None:
        s = s.dt.tz_localize("Europe/Moscow")
    else:
        s = s.dt.tz_convert("Europe/Moscow")
    return pd.DatetimeIndex(pd.to_datetime(s.astype(str).str[:10])).as_unit("ns").asi8 // NSD + EPO


conn = psycopg2.connect(DSN)
mac = pd.read_sql("SELECT secid, ts, close FROM macro_daily", conn)
mac["close"] = mac["close"].astype(float)
mac["ord"] = day_ord_naive(mac["ts"])
MAC = {}
for s, g in mac.groupby("secid"):
    g = g.sort_values("ord").drop_duplicates("ord", keep="last")
    c = pd.Series(g["close"].values)
    MAC[s] = dict(ord=g["ord"].values, r1=c.pct_change().values,
                  r5=(c / c.shift(5) - 1).values, r20=(c / c.shift(20) - 1).values)
print("MACRO secids:", sorted(MAC), "rows:", len(mac))

fd = pd.read_csv(DIR + "/feature_matrix.csv")
fd["entry_time"] = pd.to_datetime(fd["entry_time"], utc=True)
fd = fd.sort_values("entry_time").reset_index(drop=True)
e_ord = day_ord_tz(fd["entry_time"])
print("fd n=%d figis=%d e_ord %d..%d" % (len(fd), fd.figi.nunique(), e_ord.min(), e_ord.max()))
print("OLD cols:", list(fd.columns)[:40])

figis = sorted(fd["figi"].unique().tolist())


def med_gap(df):
    gs = []
    for f, g in df.groupby("figi"):
        g = g.sort_values("ts")
        d = np.diff(g["ord_"].values)
        if len(d):
            gs.append(float(np.median(d)))
    return float(np.median(gs)) if gs else 0.0


CAND = {}
MG = {}
for iv in (24, 5):
    df = pd.read_sql("SELECT figi, ts, open, high, low, close FROM candles "
                     "WHERE interval=%s AND figi = ANY(%s) AND ts >= '2025-09-01' "
                     "ORDER BY figi, ts", conn, params=(iv, figis))
    if len(df) == 0:
        print("iv %s: EMPTY" % iv)
        continue
    df["ord_"] = day_ord_tz(df["ts"])
    MG[iv] = med_gap(df)
    CAND[iv] = df
    print("iv %s: rows=%d figis=%d med_gap=%.2f" % (iv, len(df), df.figi.nunique(), MG[iv]))

parts = [CAND[iv] for iv in (24, 5) if iv in CAND and 0.75 <= MG[iv] <= 1.5]
if not parts:
    iv = max(CAND, key=lambda k: MG[k])
    print("fallback iv", iv)
    parts = [CAND[iv]]
alld = pd.concat(parts).sort_values(["figi", "ts"], kind="stable")
alld = alld.drop_duplicates(["figi", "ord_"], keep="last")
print("daily base rows=%d figis=%d" % (len(alld), alld.figi.nunique()))

DAILY = {}
for f, g in alld.groupby("figi"):
    g = g.sort_values("ord_").reset_index(drop=True)
    c = g["close"].astype(float)
    o = g["open"].astype(float)
    h = g["high"].astype(float)
    l = g["low"].astype(float)
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    hh20 = h.rolling(20).max()
    ll20 = l.rolling(20).min()
    pc = c.shift(1)
    trr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    dd = c.diff()
    au = dd.clip(lower=0.0).rolling(14).mean()
    ad = (-dd).clip(lower=0.0).rolling(14).mean()
    rsi = 100.0 - 100.0 / (1.0 + au / ad)
    DAILY[f] = dict(
        ord_=g["ord_"].values,
        d_ret1=c.pct_change().values,
        d_ret5=(c / c.shift(5) - 1).values,
        d_ret20=(c / c.shift(20) - 1).values,
        d_pos20=((c - ll20) / (hh20 - ll20).replace(0.0, np.nan)).values,
        d_atr=(trr.rolling(14).mean() / c * 100.0).values,
        d_rsi=rsi.values,
        d_slope20=(ema20 / ema20.shift(5) - 1).values,
        d_above20=(c > ema20).astype(float).values,
        d_above50=(c > ema50).astype(float).values,
        d_rng=((h - l) / c).values,
        d_body=((c - o) / c).values,
        d_vol20=c.pct_change().rolling(20).std().values,
    )
print("DAILY built for %d figis" % len(DAILY))

DK = ("d_ret1", "d_ret5", "d_ret20", "d_pos20", "d_atr", "d_rsi", "d_slope20",
      "d_above20", "d_above50", "d_rng", "d_body", "d_vol20")
for k in DK:
    fd[k] = np.nan
for k in ("z_d_ret1", "z_d_ret5", "z_d_ret20", "z_d_slope20", "z_d_body"):
    fd[k] = np.nan
missing_f = 0
for f, lab in fd.groupby("figi").groups.items():
    A = DAILY.get(f)
    if A is None:
        missing_f += 1
        continue
    pos = np.asarray(lab)
    eo = e_ord[pos]
    q = np.searchsorted(A["ord_"], eo, side="left") - 1
    same = (q >= 0) & (A["ord_"][np.clip(q, 0, None)] == eo)
    q = np.where(same, q - 1, q)
    okv = q >= 0
    qc = np.clip(q, 0, None)
    for k in DK:
        fd.loc[pos, k] = np.where(okv, A[k][qc], np.nan)
    dr = fd.loc[pos, "dir"].values.astype(float)
    for k in ("d_ret1", "d_ret5", "d_ret20", "d_slope20", "d_body"):
        fd.loc[pos, "z_" + k] = dr * fd.loc[pos, k].values.astype(float)
print("figis without daily candles:", missing_f)

MKEYS = {"IMOEX": "im", "USDRUB": "usd", "EURRUB": "eur", "CNYRUB_TOM": "cny",
         "GLDRUB_TOM": "gold", "BRENT": "brent", "NG": "ng"}
for s, pre in MKEYS.items():
    if s not in MAC:
        continue
    M = MAC[s]
    q = np.searchsorted(M["ord"], e_ord, side="left") - 1
    same = (q >= 0) & (M["ord"][np.clip(q, 0, None)] == e_ord)
    q = np.where(same, q - 1, q)
    valid = q >= 0
    qc = np.clip(q, 0, None)
    fd[pre + "_r1"] = np.where(valid, M["r1"][qc], np.nan)
    fd[pre + "_r5"] = np.where(valid, M["r5"][qc], np.nan)
    if s == "IMOEX":
        fd["im_r20"] = np.where(valid, M["r20"][qc], np.nan)

fd.to_csv(DIR + "/feature_matrix2.csv", index=False)
print("== NEW diag ==")
for c in ("d_ret1", "d_rsi", "d_pos20", "usd_r1", "usd_r5", "im_r20", "brent_r5", "ng_r1"):
    x = pd.to_numeric(fd[c], errors="coerce").values.astype(float)
    fin = x[np.isfinite(x)]
    print("NEW %-8s finite=%4d uniq=%4d mean=%+.5f" %
          (c, len(fin), len(pd.unique(fin)), fin.mean() if len(fin) else 0.0))


def spear(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.std(x[ok]) == 0:
        return np.nan
    return float(np.corrcoef(pd.Series(x[ok]).rank().values,
                             pd.Series(y[ok]).rank().values)[0, 1])


FEATS = list(DK) + ["z_d_ret1", "z_d_ret5", "z_d_ret20", "z_d_slope20", "z_d_body"] + \
    sorted({p + k for p in ("im_", "usd_", "eur_", "cny_", "gold_", "brent_", "ng_")
            for k in ("r1", "r5")} | {"im_r20"})
fd["ym"] = fd.entry_time.dt.tz_convert("Europe/Moscow").dt.strftime("%Y-%m")

for side, g in (("ALL", fd), ("BUY", fd[fd.side == "BUY"]), ("SELL", fd[fd.side == "SELL"])):
    y = g.net_pnl.values.astype(float)
    w = (y > 0).astype(float)
    print("== %s n=%d wr=%.3f terciles (top15) ==" % (side, len(g), w.mean()))
    rows = []
    for c in FEATS:
        x = pd.to_numeric(g[c], errors="coerce").values.astype(float)
        okm = np.isfinite(x)
        if okm.sum() < 30:
            continue
        ic = spear(x, y)
        rk = pd.Series(x[okm]).rank(method="first").values
        ql = pd.qcut(rk, 3, labels=False)
        top, bot = ql == 2, ql == 0
        wok, yok = w[okm], y[okm]
        rows.append((c, int(okm.sum()), ic,
                     wok[top].mean() - wok[bot].mean(),
                     yok[top].mean() - yok[bot].mean()))
    res = pd.DataFrame(rows, columns=["feature", "n", "ic", "wrS", "pnlS"])
    res = res.iloc[np.argsort(-np.nan_to_num(res.ic.abs().values))]
    for _, rr in res.head(15).iterrows():
        print("%-10s n=%4d ic=%s wrS=%+.3f pnlS=%+7.1f" %
              (rr.feature, rr.n, ("%+.3f" % rr.ic) if np.isfinite(rr.ic) else "  nan",
               rr.wrS, rr.pnlS))

CAND2 = ["usd_r5", "im_r20", "brent_r5", "ng_r1", "gold_r5", "cny_r1",
         "d_ret1", "d_pos20", "d_rsi", "d_body"]
for side in ("BUY", "SELL"):
    g = fd[fd.side == side]
    print("== monthly IC %s ==" % side)
    rows = []
    for ym, gg in g.groupby("ym"):
        yv = gg.net_pnl.values.astype(float)
        row = {"ym": ym, "n": len(gg)}
        for c in CAND2:
            v = spear(pd.to_numeric(gg[c], errors="coerce").values.astype(float), yv)
            row[c] = round(v, 2) if np.isfinite(v) else np.nan
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))
print("DONE")
