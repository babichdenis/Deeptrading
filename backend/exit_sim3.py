# -*- coding: utf-8 -*-
"""Exit sim v3: true daily-ATR14 risk (no lookahead), pessimistic fills, stop/tp semantics doc."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, psycopg2

np.seterr(divide="ignore", invalid="ignore")
DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
NS = 1_000_000_000
NSD = 86_400_000_000_000
EPO = 719163
conn = psycopg2.connect(DSN)


def msk_ord(idx):
    return pd.DatetimeIndex(idx).tz_convert("Europe/Moscow").tz_localize(None).as_unit("ns").asi8 // NSD + EPO


tr = pd.read_sql("SELECT figi, ticker, side, qty, entry_time, entry_price, stop_loss, "
                 "take_profit, exit_time, exit_price, net_pnl, exit_reason "
                 "FROM sandbox_trades WHERE test_name='tg_pool20'", conn)
for c in ("entry_time", "exit_time"):
    tr[c] = pd.to_datetime(tr[c], utc=True)
for c in ("entry_price", "stop_loss", "take_profit", "exit_price", "net_pnl"):
    tr[c] = pd.to_numeric(tr[c], errors="coerce")
tr["qty"] = pd.to_numeric(tr["qty"], errors="coerce").abs().astype(float)
tr["dir"] = np.where(tr.side.values == "BUY", 1.0, -1.0)
tr["e_ord"] = msk_ord(pd.DatetimeIndex(tr.entry_time))
tr["dur_h"] = (pd.DatetimeIndex(tr.exit_time) - pd.DatetimeIndex(tr.entry_time)).total_seconds() / 3600

print("== STOP/TP SEMANTICS ==")
st = tr[tr.stop_loss > 0]
print("stop_loss>0 n=%d sides=%s" % (len(st), st.side.value_counts().to_dict()))
if len(st):
    print("SELL stop/entry q[0,.5,1]:", np.round((st.stop_loss / st.entry_price).quantile([0, .5, 1]).values, 2))
    print(st[["ticker", "entry_price", "stop_loss"]].head(5).to_string(index=False))
tp = tr[tr.take_profit > 0]
print("take_profit>0 n=%d sides=%s" % (len(tp), tp.side.value_counts().to_dict()))
if len(tp):
    print("BUY (tp-e)/e q[0,.25,.5,.75,1]:", np.round((tp.take_profit / tp.entry_price - 1).quantile([0, .25, .5, .75, 1]).values, 3))
    print(tp[["ticker", "entry_price", "take_profit"]].head(5).to_string(index=False))

dly = pd.read_sql("SELECT figi, ts, high, low, close FROM candles WHERE interval=24 "
                  "AND ts>='2024-06-01' AND figi IN (SELECT DISTINCT figi FROM sandbox_trades "
                  "WHERE test_name='tg_pool20') ORDER BY figi, ts", conn)
ATR = {}
for f, g in dly.groupby("figi"):
    g = g.sort_values("ts").reset_index(drop=True)
    h = g.high.astype(float); l = g.low.astype(float); c = g.close.astype(float)
    pc = c.shift(1)
    trr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = trr.rolling(14, min_periods=7).mean()
    rngm = (h - l).rolling(20, min_periods=7).median()
    ATR[f] = (msk_ord(pd.DatetimeIndex(g.ts)), atr.values, rngm.values)
print("daily ATR figis=%d rows=%d" % (len(ATR), len(dly)))

risk = np.full(len(tr), np.nan); rsrc = []
for i in range(len(tr)):
    A = ATR.get(tr.figi.iloc[i])
    if A is None:
        rsrc.append("none"); continue
    o, a, rm = A
    q = int(np.searchsorted(o, tr["e_ord"].iloc[i], "left")) - 1
    v = np.nan
    if q >= 0:
        v = a[q] if np.isfinite(a[q]) else rm[q] if np.isfinite(rm[q]) else np.nan
    if not np.isfinite(v):
        v = 0.02 * float(tr.entry_price.iloc[i]); rsrc.append("2pct")
    else:
        rsrc.append("atr14")
    risk[i] = v
tr["risk"] = risk
tr["risk_pct"] = tr.risk / tr.entry_price * 100
print("risk_src=%s | risk%%/price q[.05,.5,.95]: %s" %
      (pd.Series(rsrc).value_counts().to_dict(),
       np.round(tr.risk_pct.quantile([.05, .5, .95]).values, 2)))

IV = 2
cd = pd.read_sql("SELECT figi, ts, high, low, close FROM candles WHERE interval=%s "
                 "AND ts>='2025-10-25' AND figi IN (SELECT DISTINCT figi FROM sandbox_trades "
                 "WHERE test_name='tg_pool20') ORDER BY figi, ts", conn, params=(IV,))
CD = {}
for f, g in cd.groupby("figi"):
    CD[f] = dict(ts=pd.DatetimeIndex(g.ts).as_unit("ns").asi8,
                 hi=g.high.astype(float).values, lo=g.low.astype(float).values,
                 cl=g.close.astype(float).values)
print("path candles rows=%d figis=%d" % (len(cd), len(CD)))

POL = ["LASTC", "SL1", "TP05", "TP1", "TP15", "TP2", "TP3", "TP1_SL1", "TP2_SL1",
       "TRAIL_1R05", "TRAIL_15R075", "TRAIL_2R1", "TIME12", "TIME24", "TIME48",
       "BE1R", "TP2_BE_SL1"]
res = {p: np.full(len(tr), np.nan) for p in POL}
mfeR = np.full(len(tr), np.nan); maeR = np.full(len(tr), np.nan); t1R = np.full(len(tr), np.nan)
e_i8 = pd.DatetimeIndex(tr.entry_time).as_unit("ns").asi8
x_i8 = pd.DatetimeIndex(tr.exit_time).as_unit("ns").asi8
no_path = 0
for i in range(len(tr)):
    r = tr.iloc[i]
    A = CD.get(r.figi)
    R = float(r.risk)
    if A is None or not np.isfinite(R) or R <= 0:
        no_path += 1; continue
    i0 = max(int(np.searchsorted(A["ts"], e_i8[i], "right")) - 1, 0)
    i1 = int(np.searchsorted(A["ts"], x_i8[i], "right"))
    if i1 <= i0:
        no_path += 1; continue
    hs = A["hi"][i0:i1]; ls = A["lo"][i0:i1]; cs = A["cl"][i0:i1]; tss = A["ts"][i0:i1]
    E = float(r.entry_price); D = float(r.dir); Q = float(r.qty); XP = float(r.exit_price)
    fav = np.maximum(np.where(D > 0, hs - E, E - ls), 0.0)
    adv = np.maximum(np.where(D > 0, E - ls, hs - E), 0.0)
    lastc = float(cs[-1])

    def dd(px):
        return Q * D * (px - XP)

    mfeR[i] = fav.max() / R; maeR[i] = adv.max() / R
    m1 = fav >= R
    if m1.any():
        t1R[i] = (tss[int(np.argmax(m1))] - e_i8[i]) / NS / 3600
    res["LASTC"][i] = dd(lastc)
    m = adv >= R
    res["SL1"][i] = dd(E - D * R) if m.any() else dd(lastc)
    for k, lab in ((0.5, "TP05"), (1.0, "TP1"), (1.5, "TP15"), (2.0, "TP2"), (3.0, "TP3")):
        m = fav >= k * R
        res[lab][i] = dd(E + D * k * R) if m.any() else dd(lastc)
    for kt, lab in ((1.0, "TP1_SL1"), (2.0, "TP2_SL1")):
        px = None
        for b in range(len(cs)):
            if adv[b] >= R:
                px = E - D * R; break
            if fav[b] >= kt * R:
                px = E + D * kt * R; break
        res[lab][i] = dd(px) if px is not None else dd(lastc)
    for arm, giv, lab in ((1.0, 0.5, "TRAIL_1R05"), (1.5, 0.75, "TRAIL_15R075"), (2.0, 1.0, "TRAIL_2R1")):
        peak = -1e18; level = np.nan; px = lastc
        for b in range(len(cs)):
            if np.isfinite(level):
                hit = ls[b] <= level if D > 0 else hs[b] >= level
                if hit:
                    px = level; break
            if fav[b] > peak:
                peak = fav[b]
            if peak >= arm * R and not np.isfinite(level):
                level = E + D * (peak - giv * R)
        res[lab][i] = dd(px)
    for H, lab in ((12, "TIME12"), (24, "TIME24"), (48, "TIME48")):
        m = tss >= e_i8[i] + H * 3600 * NS
        res[lab][i] = dd(cs[int(np.argmax(m))] if m.any() else lastc)
    j = np.argmax(fav >= R) if (fav >= R).any() else -1
    if j < 0:
        res["BE1R"][i] = dd(lastc)
    else:
        px = lastc
        for b in range(j + 1, len(cs)):
            if (D > 0 and ls[b] <= E) or (D < 0 and hs[b] >= E):
                px = E; break
        res["BE1R"][i] = dd(px)
    armed = False; px = None
    for b in range(len(cs)):
        if adv[b] >= R:
            px = E - D * R; break
        if armed and ((D > 0 and ls[b] <= E) or (D < 0 and hs[b] >= E)):
            px = E; break
        if fav[b] >= 2 * R:
            px = E + D * 2 * R; break
        if fav[b] >= R:
            armed = True
    res["TP2_BE_SL1"][i] = dd(px) if px is not None else dd(lastc)

print("no_path=%d" % no_path)
tr["ym"] = tr.entry_time.dt.strftime("%Y-%m")
tr["half"] = np.where(tr.ym <= "2026-06", "IS", "OOS")
for p in POL:
    tr["d_" + p] = res[p]
print("== policy deltas vs actual, rub/trade ==")
for p in POL:
    d = tr["d_" + p]; ok = np.isfinite(d)
    if not ok.any():
        continue
    new = tr.net_pnl.where(~ok, tr.net_pnl[ok] + d[ok])
    print("%-12s ok=%4d meanD=%+7.2f sumD=%+9.0f | BUY %+.2f SELL %+.2f | IS %+.2f OOS %+.2f | wr %.3f->%.3f | newAvg %+.2f" %
          (p, ok.sum(), d[ok].mean(), d[ok].sum(),
           d[ok & (tr.side == "BUY")].mean(), d[ok & (tr.side == "SELL")].mean(),
           d[ok & (tr.half == "IS")].mean(), d[ok & (tr.half == "OOS")].mean(),
           (tr.net_pnl[ok] > 0).mean(), (new[ok] > 0).mean(), new[ok].mean()))
print("== detail: new pnl by side x half for key policies ==")
for p in ("TP2_SL1", "TRAIL_15R075", "TP2_BE_SL1"):
    for s in ("BUY", "SELL"):
        row = []
        for h in ("IS", "OOS"):
            m = np.isfinite(tr["d_" + p]) & (tr.side == s) & (tr.half == h)
            new = tr.net_pnl[m] + tr["d_" + p][m]
            row.append("%s n=%4d avg=%+7.2f sum=%+8.0f wr=%.2f" % (h, m.sum(), new.mean(), new.sum(), (new > 0).mean()))
        print("%-12s %s | %s | %s" % (p, s, row[0], row[1]))
print("== MFE/MAE true-R ==")
tr["mfeR"] = mfeR
tr["maeR"] = maeR
tr["t1R"] = t1R
tr["realR"] = tr.net_pnl / (tr.risk * tr.qty)
ok = np.isfinite(tr.mfeR); rr = tr[ok].copy(); rr["win"] = rr.net_pnl > 0
print(rr.groupby(["side", "win"])[["mfeR", "maeR", "realR", "dur_h", "t1R"]].mean().round(2).to_string())
for s in ("BUY", "SELL"):
    g = rr[rr.side == s]
    print("%s: MFE>=1R %.2f | >=2R %.2f | >=2R&real<1R %.2f | med t1R=%.1fh | giveback(mfeR-max(realR,0))=%.2f" %
          (s, (g.mfeR >= 1).mean(), (g.mfeR >= 2).mean(), ((g.mfeR >= 2) & (g.realR < 1)).mean(),
           g.t1R.median(), (g.mfeR - g.realR.clip(lower=0)).mean()))
tr.to_csv("X:/Deeptrading/backend/reports/feature_scan/exit_sim3.csv", index=False)
print("DONE")
