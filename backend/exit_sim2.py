# -*- coding: utf-8 -*-
"""Exit sim v2: stop semantics diag, hl288 risk proxy, combined TP/SL, LASTC baseline."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, psycopg2

np.seterr(divide="ignore", invalid="ignore")
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NS = 1_000_000_000
conn = psycopg2.connect(DSN)

tr = pd.read_sql("SELECT figi, ticker, side, qty, entry_time, entry_price, stop_loss, "
                 "take_profit, exit_time, exit_price, net_pnl, exit_reason "
                 "FROM sandbox_trades WHERE test_name='tg_pool20'", conn)
for c in ("entry_time", "exit_time"):
    tr[c] = pd.to_datetime(tr[c], utc=True)
for c in ("entry_price", "stop_loss", "take_profit", "exit_price", "net_pnl"):
    tr[c] = pd.to_numeric(tr[c], errors="coerce")
tr["qty"] = pd.to_numeric(tr["qty"], errors="coerce").abs().astype(float)
tr["dir"] = np.where(tr.side.values == "BUY", 1.0, -1.0)
tr["dur_h"] = (pd.DatetimeIndex(tr.exit_time) - pd.DatetimeIndex(tr.entry_time)).total_seconds() / 3600

print("== STOP SEMANTICS DIAG ==")
st = tr[tr.stop_loss > 0]
print("stop>0: n=%d | by side: %s" % (len(st), st.side.value_counts().to_dict()))
print("stop/entry q[0,.25,.5,.75,1]:", np.round((st.stop_loss / st.entry_price).quantile([0, .25, .5, .75, 1]).values, 3))
rel = (st.stop_loss - st.entry_price).abs() / st.entry_price
print("|stop-e|/e  q[0,.25,.5,.75,1]:", np.round(rel.quantile([0, .25, .5, .75, 1]).values, 4))
for s in ("BUY", "SELL"):
    g = st[st.side == s]
    if len(g):
        print("%s: n=%d stop>e share=%.2f med|s-e|/e=%.4f" %
              (s, len(g), (g.stop_loss > g.entry_price).mean(),
               ((g.stop_loss - g.entry_price).abs() / g.entry_price).median()))
print("take_profit>0: n=%d" % (tr.take_profit > 0).sum())
print("entry_price q[0,.25,.5,.75,1]:", np.round(tr.entry_price.quantile([0, .25, .5, .75, 1]).values, 2))
print("qty q[.5,.95,.99]:", np.round(tr.qty.quantile([.5, .95, .99]).values, 0))

IV = 2
cd = pd.read_sql("SELECT figi, ts, high, low, close FROM candles WHERE interval=%s "
                 "AND ts>='2025-10-25' AND figi IN (SELECT DISTINCT figi FROM sandbox_trades "
                 "WHERE test_name='tg_pool20') ORDER BY figi, ts", conn, params=(IV,))
CD = {}
for f, g in cd.groupby("figi"):
    hl = (g.high.astype(float) - g.low.astype(float)).values
    med = pd.Series(hl).rolling(288, min_periods=96).median().values
    CD[f] = dict(ts=pd.DatetimeIndex(g.ts).as_unit("ns").asi8,
                 hi=g.high.astype(float).values, lo=g.low.astype(float).values,
                 cl=g.close.astype(float).values, med=med)
print("candles rows=%d figis=%d" % (len(cd), len(CD)))

POL = ["LASTC", "TP1", "TP15", "TP2", "TP3", "TP1_SL1", "TP2_SL1",
       "TRAIL_1R05", "TRAIL_15R075", "TIME12", "TIME24", "TIME48", "BE1R"]
res = {p: np.full(len(tr), np.nan) for p in POL}
mfeR = np.full(len(tr), np.nan)
maeR = np.full(len(tr), np.nan)
risk_u = np.full(len(tr), np.nan)
risk_src = []
e_i8 = pd.DatetimeIndex(tr.entry_time).as_unit("ns").asi8
x_i8 = pd.DatetimeIndex(tr.exit_time).as_unit("ns").asi8
no_path = 0
for i in range(len(tr)):
    r = tr.iloc[i]
    A = CD.get(r.figi)
    if A is None:
        no_path += 1
        continue
    i0 = int(np.searchsorted(A["ts"], e_i8[i], "right")) - 1
    if i0 < 0:
        i0 = 0
    i1 = int(np.searchsorted(A["ts"], x_i8[i], "right"))
    if i1 <= i0:
        no_path += 1
        continue
    hs = A["hi"][i0:i1]; ls = A["lo"][i0:i1]; cs = A["cl"][i0:i1]; tss = A["ts"][i0:i1]
    E = float(r.entry_price); D = float(r.dir); Q = float(r.qty)
    fav = np.maximum(np.where(D > 0, hs - E, E - ls), 0.0)
    adv = np.maximum(np.where(D > 0, E - ls, hs - E), 0.0)
    src = "none"; risk = np.nan
    if np.isfinite(r.stop_loss) and r.stop_loss > 0:
        rd = abs(float(r.stop_loss) - E) / E
        if 0.002 <= rd <= 0.25:
            risk = abs(float(r.stop_loss) - E); src = "stop"
    if not np.isfinite(risk):
        m = A["med"][i0] if i0 < len(A["med"]) else np.nan
        if np.isfinite(m) and m > 0:
            risk = float(m); src = "hl288"
        else:
            risk = 0.01 * E; src = "1pct"
    risk_u[i] = risk
    risk_src.append(src)
    mfeR[i] = float(np.nanmax(fav)) / risk
    maeR[i] = float(np.nanmax(adv)) / risk
    lastc = float(cs[-1]); XP = float(r.exit_price)

    def dd(px):
        return Q * D * (px - XP)

    res["LASTC"][i] = dd(lastc)
    for k, lab in ((1.0, "TP1"), (1.5, "TP15"), (2.0, "TP2"), (3.0, "TP3")):
        m = fav >= k * risk
        res[lab][i] = dd(E + D * k * risk) if m.any() else dd(lastc)
    for kt, lab in ((1.0, "TP1_SL1"), (2.0, "TP2_SL1")):
        px = None
        for b in range(len(cs)):
            if adv[b] >= risk:
                px = E - D * risk
                break
            if fav[b] >= kt * risk:
                px = E + D * kt * risk
                break
        res[lab][i] = dd(px) if px is not None else dd(lastc)
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
                level = E + D * (peak - giv * risk)
        res[lab][i] = dd(px)
    for H, lab in ((12, "TIME12"), (24, "TIME24"), (48, "TIME48")):
        m = tss >= e_i8[i] + H * 3600 * NS
        res[lab][i] = dd(cs[int(np.argmax(m))] if m.any() else lastc)
    m1 = fav >= risk
    if m1.any():
        j = int(np.argmax(m1))
        hitb = False
        for b in range(j + 1, len(cs)):
            if (D > 0 and ls[b] <= E) or (D < 0 and hs[b] >= E):
                hitb = True
                break
        res["BE1R"][i] = dd(E) if hitb else dd(lastc)
    else:
        res["BE1R"][i] = dd(lastc)

print("no_path=%d risk_src=%s" % (no_path, pd.Series(risk_src).value_counts().to_dict()))
tr["ym"] = tr.entry_time.dt.strftime("%Y-%m")
tr["half"] = np.where(tr.ym <= "2026-06", "IS", "OOS")
tr["risk"] = risk_u; tr["mfeR"] = mfeR; tr["maeR"] = maeR
for p in POL:
    tr["d_" + p] = res[p]

print("== policy deltas vs actual, rub/trade ==")
for p in POL:
    d = tr["d_" + p]; ok = np.isfinite(d)
    if not ok.any():
        continue
    new = tr.net_pnl.where(~ok, tr.net_pnl[ok] + d[ok])
    print("%-12s ok=%4d meanD=%+7.2f sumD=%+9.0f | BUY %+.2f SELL %+.2f | IS %+.2f OOS %+.2f | wr %.3f->%.3f" %
          (p, ok.sum(), d[ok].mean(), d[ok].sum(),
           d[ok & (tr.side == "BUY")].mean(), d[ok & (tr.side == "SELL")].mean(),
           d[ok & (tr.half == "IS")].mean(), d[ok & (tr.half == "OOS")].mean(),
           (tr.net_pnl[ok] > 0).mean(), (new[ok] > 0).mean()))

print("== MFE/MAE (R units, fixed risk) ==")
tr["realR"] = tr.net_pnl / (tr.risk * tr.qty)
ok = np.isfinite(tr.mfeR)
rr = tr[ok].copy()
rr["win"] = rr.net_pnl > 0
print(rr.groupby(["side", "win"])[["mfeR", "maeR", "realR", "dur_h"]].mean().round(2).to_string())
for s in ("BUY", "SELL"):
    g = rr[rr.side == s]
    print("%s: MFE>=1R %.2f | MFE>=2R %.2f | MFE>=2R&real<1R %.2f | med|exit-close_gap|/e %.4f" %
          (s, (g.mfeR >= 1).mean(), (g.mfeR >= 2).mean(),
           ((g.mfeR >= 2) & (g.realR < 1)).mean(),
           (g["d_LASTC"].abs() / (g.qty * g.entry_price)).median()))
tr.to_csv("X:/Deeptrading/backend/reports/feature_scan/exit_sim2.csv", index=False)
print("DONE")
