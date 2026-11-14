# -*- coding: utf-8 -*-
"""Exit-side analysis: MFE/MAE + alternative exit policies on tg_pool20."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, psycopg2

np.seterr(divide="ignore", invalid="ignore")
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NS = 1_000_000_000
conn = psycopg2.connect(DSN)

tr = pd.read_sql("SELECT figi, ticker, side, qty, entry_time, entry_price, stop_loss, "
                 "take_profit, exit_time, exit_price, net_pnl, exit_reason, trailing_active "
                 "FROM sandbox_trades WHERE test_name='tg_pool20'", conn)
for c in ("entry_time", "exit_time"):
    tr[c] = pd.to_datetime(tr[c], utc=True)
for c in ("entry_price", "stop_loss", "take_profit", "exit_price", "net_pnl"):
    tr[c] = pd.to_numeric(tr[c], errors="coerce")
tr["qty"] = pd.to_numeric(tr["qty"], errors="coerce").astype(float)
tr["dir"] = np.where(tr["side"].values == "BUY", 1.0, -1.0)
tr["dur_h"] = (pd.DatetimeIndex(tr.exit_time) - pd.DatetimeIndex(tr.entry_time)).total_seconds() / 3600
print("trades n=%d BUY=%d SELL=%d dur_h med=%.1f" %
      (len(tr), (tr.side == "BUY").sum(), (tr.side == "SELL").sum(), tr.dur_h.median()))
print("stop_loss>0: %d (%.2f)" % ((tr.stop_loss > 0).sum(), (tr.stop_loss > 0).mean()))
print("exit_reason counts:")
print(tr.exit_reason.value_counts(dropna=False).head(12).to_string())

rows = []
for iv in (1, 2, 3, 4, 5, 8, 10, 11):
    n, nf = pd.read_sql("SELECT count(*)::bigint n, count(distinct figi) nf FROM candles "
                        "WHERE interval=%s AND ts>='2025-10-25'", conn, params=(iv,)).values[0]
    med = np.nan
    if n:
        tf = pd.read_sql("SELECT figi FROM candles WHERE interval=%s AND ts>='2025-10-25' "
                         "GROUP BY figi ORDER BY count(*) DESC LIMIT 1", conn, params=(iv,))
        if len(tf):
            gts = pd.read_sql("SELECT ts FROM candles WHERE interval=%s AND figi=%s "
                              "AND ts>='2026-09-01' ORDER BY ts", conn,
                              params=(iv, tf["figi"].iloc[0]))
            d = np.diff(pd.DatetimeIndex(gts.ts).as_unit("ns").asi8)
            med = np.median(d) / NS if len(d) else np.nan
    rows.append((iv, int(n), int(nf), med))
pr = pd.DataFrame(rows, columns=["iv", "n", "nf", "med_s"])
print(pr.to_string(index=False))
c1 = pr[(pr.med_s >= 50) & (pr.med_s <= 900) & (pr.nf >= 26) & (pr.n <= 2500000)]
if len(c1):
    IV = int(c1.sort_values("med_s").iv.iloc[0])
else:
    c2 = pr[(pr.med_s <= 3600) & (pr.nf >= 26) & (pr.n <= 2500000)]
    IV = int(c2.sort_values("med_s").iv.iloc[0]) if len(c2) else int(pr[pr.nf >= 26].sort_values("med_s").iv.iloc[0])
print("PICKED interval", IV)

cd = pd.read_sql("SELECT figi, ts, high, low, close FROM candles WHERE interval=%s "
                 "AND ts>='2025-10-25' AND figi IN (SELECT DISTINCT figi FROM sandbox_trades "
                 "WHERE test_name='tg_pool20') ORDER BY figi, ts", conn, params=(IV,))
CD = {}
for f, g in cd.groupby("figi"):
    CD[f] = dict(ts=pd.DatetimeIndex(g.ts).as_unit("ns").asi8,
                 hi=g.high.astype(float).values, lo=g.low.astype(float).values,
                 cl=g.close.astype(float).values)
print("candles loaded %d rows, %d figis" % (len(cd), len(CD)))

recs = tr.reset_index(drop=True)
e_i8 = pd.DatetimeIndex(recs.entry_time).as_unit("ns").asi8
x_i8 = pd.DatetimeIndex(recs.exit_time).as_unit("ns").asi8
POL = ["ACT", "TP1", "TP15", "TP2", "TP3", "TRAIL_1R05", "TRAIL_15R075",
       "TIME12", "TIME24", "TIME48", "BE1R"]
res = {p: np.full(len(recs), np.nan) for p in POL}
mfeR = np.full(len(recs), np.nan)
maeR = np.full(len(recs), np.nan)
t1r_h = np.full(len(recs), np.nan)
no_path = 0
for i in range(len(recs)):
    r = recs.iloc[i]
    A = CD.get(r.figi)
    if A is None:
        no_path += 1
        continue
    i0 = np.searchsorted(A["ts"], e_i8[i], "right") - 1
    if i0 < 0:
        i0 = 0
    i1 = np.searchsorted(A["ts"], x_i8[i], "right")
    if i1 <= i0:
        no_path += 1
        continue
    hs = A["hi"][i0:i1]; ls = A["lo"][i0:i1]; cs = A["cl"][i0:i1]; tss = A["ts"][i0:i1]
    E = float(r.entry_price); D = float(r.dir)
    Q = float(r.qty) if np.isfinite(r.qty) else 0.0
    risk = abs(float(r.stop_loss) - E) if np.isfinite(r.stop_loss) and r.stop_loss > 0 else np.nan
    fav = np.where(D > 0, hs - E, E - ls)
    adv = np.where(D > 0, E - ls, hs - E)
    MFE = max(0.0, float(np.nanmax(fav)))
    MAE = max(0.0, float(np.nanmax(adv)))
    if np.isfinite(risk) and risk > 0:
        mfeR[i] = MFE / risk
        maeR[i] = MAE / risk
        m1 = fav >= risk
        if m1.any():
            t1r_h[i] = (tss[int(np.argmax(m1))] - e_i8[i]) / NS / 3600
    lastc = float(cs[-1]); XP = float(r.exit_price)
    res["ACT"][i] = 0.0

    def dd(px):
        return Q * D * (px - XP)

    if np.isfinite(risk) and risk > 0:
        for k, lab in ((1.0, "TP1"), (1.5, "TP15"), (2.0, "TP2"), (3.0, "TP3")):
            m = fav >= k * risk
            res[lab][i] = dd(E + D * k * risk) if m.any() else dd(lastc)
        for arm, giv, lab in ((1.0, 0.5, "TRAIL_1R05"), (1.5, 0.75, "TRAIL_15R075")):
            peak = -1e18; level = np.nan; px = lastc
            for b in range(len(cs)):
                if np.isfinite(level):
                    hit = ls[b] <= level if D > 0 else hs[b] >= level
                    if hit:
                        px = level
                        break
                if fav[b] > peak:
                    peak = fav[b]
                if peak >= arm * risk and not np.isfinite(level):
                    level = E + D * (peak - giv * risk) if D > 0 else E - (peak - giv * risk)
            res[lab][i] = dd(px)
        for H, lab in ((12, "TIME12"), (24, "TIME24"), (48, "TIME48")):
            m = tss >= e_i8[i] + H * 3600 * NS
            res[lab][i] = dd(cs[int(np.argmax(m))] if m.any() else lastc)
        if mfeR[i] >= 1.0 and float(r.net_pnl) < 0:
            res["BE1R"][i] = Q * D * (E - XP)
        else:
            res["BE1R"][i] = 0.0
print("no_path:", no_path)

recs["ym"] = recs.entry_time.dt.strftime("%Y-%m")
recs["half"] = np.where(recs.ym <= "2026-06", "IS", "OOS")
for p in POL[1:]:
    recs["d_" + p] = res[p]
print("== policy deltas vs actual (per trade) ==")
for p in POL[1:]:
    d = recs["d_" + p]; ok = np.isfinite(d)
    if ok.sum() == 0:
        continue
    print("%-13s ok=%4d meanD=%+7.2f sumD=%+8.0f imp=%.2f | BUY %+.2f SELL %+.2f | OOS %+.2f" %
          (p, ok.sum(), d[ok].mean(), d[ok].sum(), (d[ok] > 0).mean(),
           d[ok & (recs.side == "BUY")].mean(), d[ok & (recs.side == "SELL")].mean(),
           d[ok & (recs.half == "OOS")].mean()))
print("== detail by side x half ==")
for p in ("TP2", "TRAIL_1R05", "TIME24", "BE1R"):
    for s in ("BUY", "SELL"):
        for h in ("IS", "OOS"):
            m = np.isfinite(recs["d_" + p]) & (recs.side == s) & (recs.half == h)
            if m.sum():
                print("%-13s %s %s n=%4d meanD=%+7.2f wr_new=%.2f" %
                      (p, s, h, m.sum(), recs["d_" + p][m].mean(),
                       (recs.net_pnl[m] + recs["d_" + p][m] > 0).mean()))

risk_all = np.where((recs.stop_loss > 0).values, np.abs(recs.stop_loss.values - recs.entry_price.values), np.nan)
recs["risk"] = risk_all
recs["mfeR"] = mfeR
recs["maeR"] = maeR
recs["t1r_h"] = t1r_h
rr = recs[np.isfinite(recs.mfeR)].copy()
rr["win"] = rr.net_pnl > 0
rr["realR"] = rr.net_pnl / (rr.risk * rr.qty)
print("== MFE/MAE by outcome ==")
print(rr.groupby(["side", "win"])[["mfeR", "maeR", "realR", "dur_h", "t1r_h"]].mean().to_string())
print("giveback (mfeR-realR) mean=%.2f | MFE>=2R & real<1R share=%.2f | MFE>=1R share=%.2f" %
      ((rr.mfeR - rr.realR).mean(), ((rr.mfeR >= 2) & (rr.realR < 1)).mean(), (rr.mfeR >= 1).mean()))
print("== by exit_reason ==")
g = rr.groupby("exit_reason").agg(n=("net_pnl", "size"), avg_net=("net_pnl", "mean"),
                                   mfeR=("mfeR", "mean"), realR=("realR", "mean"), maeR=("maeR", "mean"))
print(g.to_string())
recs.to_csv("X:/Deeptrading/backend/reports/feature_scan/exit_sim.csv", index=False)
print("DONE")
