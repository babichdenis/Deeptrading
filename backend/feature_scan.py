# -*- coding: utf-8 -*-
"""Entry feature scan for tg_pool20: multi-TF candles + macro vs net_pnl (no lookahead)."""
import os
import numpy as np
import pandas as pd
import psycopg2

np.seterr(divide="ignore", invalid="ignore")

DSN = "host=127.0.0.1 port=5432 dbname=deeptrading user=deeptrading"
TEST = "tg_pool20"
MSK = "Europe/Moscow"
OUTDIR = "X:/Deeptrading/backend/reports/feature_scan"
START = "2025-09-01"
NSD = 86_400_000_000_000
H3 = 3 * 3_600_000_000_000
EPO = 719163


def rsi_arr(close, n=14):
    s = pd.Series(np.asarray(close, dtype=float))
    d = s.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.rolling(n).mean()
    ad = dn.rolling(n).mean()
    out = 100.0 - 100.0 / (1.0 + au / ad)
    out = out.where(ad != 0, np.where(au > 0, 100.0, 50.0))
    return out


def atr_pct(h, l, c, n=14):
    hh = pd.Series(h)
    ll = pd.Series(l)
    cc = pd.Series(c)
    pc = cc.shift(1)
    tr = pd.concat([hh - ll, (hh - pc).abs(), (ll - pc).abs()], axis=1).max(axis=1)
    return (tr.rolling(n).mean() / cc * 100.0).values


def prep_daily(df):
    df = df.sort_values("ts").reset_index(drop=True)
    di = pd.DatetimeIndex(df["ts"])
    df["ord"] = (di.tz_convert(MSK).asi8 + H3) // NSD + EPO
    df = df.drop_duplicates("ord", keep="last").reset_index(drop=True)
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    hh20 = h.rolling(20).max()
    ll20 = l.rolling(20).min()
    return dict(
        d_ord=df["ord"].values,
        d_close=c.values,
        d_ret1=c.pct_change().values,
        d_ret5=(c / c.shift(5) - 1).values,
        d_ret20=(c / c.shift(20) - 1).values,
        d_pos20=((c - ll20) / (hh20 - ll20).replace(0.0, np.nan)).values,
        d_atr=atr_pct(h.values, l.values, c.values),
        d_rsi=rsi_arr(c.values).values,
        d_slope20=(ema20 / ema20.shift(5) - 1).values,
        d_above20=(c > ema20).astype(float).values,
        d_above50=(c > ema50).astype(float).values,
        d_rng=((h - l) / c).values,
        d_body=((c - o) / c).values,
        d_vol20=c.pct_change().rolling(20).std().values,
    )


def prep_intra(df, step_min):
    df = df.sort_values("ts").reset_index(drop=True)
    di = pd.DatetimeIndex(df["ts"])
    done = (di + pd.Timedelta(minutes=step_min)).asi8
    c = df["close"].astype(float)
    ema20 = c.ewm(span=20, adjust=False).mean()
    v = df["volume"].astype(float)
    vma = v.rolling(20).mean()
    return dict(
        tsdone=done,
        i_ord=(di.tz_convert(MSK).asi8 + H3) // NSD + EPO,
        i_close=c.values,
        i_open=df["open"].astype(float).values,
        i_high=df["high"].astype(float).values,
        i_low=df["low"].astype(float).values,
        i_ret1=c.pct_change().values,
        i_rsi=rsi_arr(c.values).values,
        i_ema20=ema20.values,
        i_vrel=(v / vma.replace(0.0, np.nan)).values,
    )


def load_iv(conn, iv, figis):
    q = ("SELECT figi, ts, open, high, low, close, volume FROM candles "
         "WHERE interval=%s AND figi = ANY(%s) AND ts >= %s ORDER BY figi, ts")
    return pd.read_sql(q, conn, params=(iv, figis, START))


def main():
    conn = psycopg2.connect(DSN)
    tr = pd.read_sql(
        "SELECT figi, ticker, side, entry_time, net_pnl, exit_reason FROM sandbox_trades "
        "WHERE test_name=%s", conn, params=(TEST,))
    tr["entry_time"] = pd.to_datetime(tr["entry_time"], utc=True)
    tr["net_pnl"] = tr["net_pnl"].astype(float)
    tr["dir"] = np.where(tr["side"].values == "BUY", 1.0, -1.0)
    figis = sorted(tr["figi"].unique().tolist())
    print("trades", len(tr), "figis", len(figis))

    parts = []
    for iv in (24, 5):
        df = load_iv(conn, iv, figis)
        print("daily iv", iv, "rows", len(df))
        parts.append(df)
    h1 = load_iv(conn, 4, figis)
    print("h1 rows", len(h1))
    m10 = load_iv(conn, 8, figis)
    print("m10 rows", len(m10))
    m5 = load_iv(conn, 2, figis)
    print("m5 rows", len(m5))

    daily = {}
    for f, g in pd.concat(parts).groupby("figi"):
        daily[f] = prep_daily(g)

    def prep_map(df, step):
        return {f: prep_intra(g, step) for f, g in df.groupby("figi")}

    H1 = prep_map(h1, 60)
    M10 = prep_map(m10, 10)
    M5 = prep_map(m5, 5)

    mac = pd.read_sql("SELECT secid, ts, close FROM macro_daily", conn)
    mac["close"] = mac["close"].astype(float)
    mac["ord"] = pd.DatetimeIndex(pd.to_datetime(mac["ts"])).asi8 // NSD + EPO
    MAC = {}
    for s, g in mac.groupby("secid"):
        g = g.sort_values("ord")
        c = pd.Series(g["close"].values)
        MAC[s] = dict(ord=g["ord"].values, r1=c.pct_change().values,
                      r5=(c / c.shift(5) - 1).values, r20=(c / c.shift(20) - 1).values)
    MKEYS = {"IMOEX": "im", "USDRUB": "usd", "EURRUB": "eur", "CNYRUB_TOM": "cny",
             "GLDRUB_TOM": "gold", "BRENT": "brent", "NG": "ng"}
    print("macro secids", sorted(MAC.keys()))

    DK = ("d_ret1", "d_ret5", "d_ret20", "d_pos20", "d_atr", "d_rsi", "d_slope20",
          "d_above20", "d_above50", "d_rng", "d_body", "d_vol20")
    DIRK = ("d_ret1", "d_ret5", "d_ret20", "d_slope20", "d_body",
            "h_ret3", "h_ret6", "m5_day", "m5_ret12", "m10_ret3")

    rows = []
    for r in tr.itertuples(index=False):
        e = r.entry_time
        e_ns = pd.DatetimeIndex([e]).asi8[0]
        e_ord = (e_ns + H3) // NSD + EPO
        e_msk = e.tz_convert(MSK)
        f = {"figi": r.figi, "ticker": r.ticker, "side": r.side,
             "net_pnl": r.net_pnl, "entry_time": e, "dir": r.dir,
             "f_hour": float(e_msk.hour), "f_hm": float(e_msk.hour + e_msk.minute / 60.0),
             "f_dow": float(e_msk.dayofweek)}
        A = daily.get(r.figi)
        p = -1
        if A is not None:
            p = int(np.searchsorted(A["d_ord"], e_ord, side="left")) - 1
            if p >= 0:
                for k in DK:
                    f[k] = float(A[k][p])

        def done_idx(D):
            if D is None:
                return -1
            return int(np.searchsorted(D["tsdone"], e_ns, side="left")) - 1

        i = done_idx(H1.get(r.figi))
        if i >= 14:
            D = H1[r.figi]
            c = D["i_close"]
            f["h_ret1"] = c[i] / c[i - 1] - 1
            f["h_ret3"] = c[i] / c[i - 3] - 1
            f["h_ret6"] = c[i] / c[i - 6] - 1
            f["h_rsi"] = D["i_rsi"][i]
            f["h_vrel"] = D["i_vrel"][i]
            f["h_dema"] = c[i] / D["i_ema20"][i] - 1
            hh = D["i_high"][max(0, i - 23):i + 1].max()
            ll = D["i_low"][max(0, i - 23):i + 1].min()
            if hh > ll:
                f["h_pos24"] = (c[i] - ll) / (hh - ll)
        i = done_idx(M10.get(r.figi))
        if i >= 14:
            D = M10[r.figi]
            c = D["i_close"]
            f["m10_ret3"] = c[i] / c[i - 3] - 1
            f["m10_ret12"] = c[i] / c[i - 12] - 1
            f["m10_rsi"] = D["i_rsi"][i]
        i = done_idx(M5.get(r.figi))
        if i >= 14:
            D = M5[r.figi]
            c = D["i_close"]
            f["m5_ret3"] = c[i] / c[i - 3] - 1
            f["m5_ret12"] = c[i] / c[i - 12] - 1
            f["m5_rsi"] = D["i_rsi"][i]
            j = int(np.searchsorted(D["i_ord"], e_ord, side="left"))
            base = np.nan
            if j < len(D["i_ord"]) and D["i_ord"][j] == e_ord:
                base = D["i_open"][j]
                if A is not None and p >= 0 and base > 0:
                    f["m5_gap"] = base / A["d_close"][p] - 1
            elif A is not None and p >= 0:
                base = A["d_close"][p]
            if np.isfinite(base) and base > 0:
                f["m5_day"] = c[i] / base - 1

        for s, pre in MKEYS.items():
            M = MAC.get(s)
            if M is None:
                continue
            q = int(np.searchsorted(M["ord"], e_ord, side="left")) - 1
            if q >= 0:
                f[pre + "_r1"] = float(M["r1"][q])
                f[pre + "_r5"] = float(M["r5"][q])
                if s == "IMOEX":
                    f["im_r20"] = float(M["r20"][q])
                if s == "BRENT":
                    f["brent_r5"] = float(M["r5"][q])
        for k in DIRK:
            if k in f and np.isfinite(f[k]):
                f["z_" + k] = r.dir * f[k]
        rows.append(f)

    fd = pd.DataFrame(rows)
    META = {"figi", "ticker", "side", "entry_time", "net_pnl", "dir", "exit_reason"}

    def scan(g, minn):
        y = g["net_pnl"].values.astype(float)
        w = (y > 0).astype(float)
        out = []
        for col in g.columns:
            if col in META:
                continue
            x = pd.to_numeric(g[col], errors="coerce").values.astype(float)
            ok = np.isfinite(x) & np.isfinite(y)
            n = int(ok.sum())
            if n < minn or np.std(x[ok]) == 0:
                continue
            xs, ys, ws = x[ok], y[ok], w[ok]
            rk = pd.Series(xs).rank().values
            ic = float(np.corrcoef(rk, pd.Series(ys).rank().values)[0, 1])
            ql = pd.qcut(pd.Series(xs).rank(method="first"), 3, labels=False).values
            top = ql == 2
            bot = ql == 0
            out.append((col, n, round(ic, 3),
                        round(ws[top].mean() - ws[bot].mean(), 3),
                        round(ys[top].mean() - ys[bot].mean(), 2),
                        round(ws[top].mean(), 3), round(ws[bot].mean(), 3)))
        res = pd.DataFrame(out, columns=["feature", "n", "ic", "wr_spread", "pnl_spread", "wr_top", "wr_bot"])
        return res.sort_values("ic", key=lambda s: s.abs(), ascending=False)

    os.makedirs(OUTDIR, exist_ok=True)
    fd.to_csv(OUTDIR + "/feature_matrix.csv", index=False)

    for name, g, minn in (("ALL", fd, 120), ("BUY", fd[fd.side == "BUY"], 60), ("SELL", fd[fd.side == "SELL"], 60)):
        y = g["net_pnl"].values
        print("== %s n=%d wr=%.3f mean_pnl=%.2f ==" % (name, len(g), (y > 0).mean(), y.mean()))
        res = scan(g, minn)
        res.to_csv(OUTDIR + "/scan_%s.csv" % name.lower(), index=False)
        for _, rr in res.head(25).iterrows():
            print("%-14s n=%4d ic=%+.3f wrS=%+.3f pnlS=%+8.1f wrT=%.2f wrB=%.2f" %
                  (rr.feature, rr.n, rr.ic, rr.wr_spread, rr.pnl_spread, rr.wr_top, rr.wr_bot))
    print("DONE")


if __name__ == "__main__":
    main()
