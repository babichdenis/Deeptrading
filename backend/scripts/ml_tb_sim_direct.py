#!/usr/bin/env python3
"""Вариант B: самостоятельные 1m-входы ml_tb (top-2% keep).
Вход по close бара, SL/TP = 2/4 * ATR14, горизонт 60 баров, одна позиция на тикер.
Отчёт: net, trades, wins/losses, PF, WR, avg win/loss, max DD (независимые счета 10k/тикер).
"""
import sys, os, json, time
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd, joblib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sqlalchemy import text
from app.database import SessionLocal

TZ = timezone.utc
OOS_FROM = datetime(2026, 8, 15, 0, 0, tzinfo=TZ)
OOS_TO = datetime(2026, 9, 13, 0, 0, tzinfo=TZ)
TP_ATR, SL_ATR, HOR, ATR_P = 4.0, 2.0, 60, 14
CAP_PER_TICKER = 10000.0
COMM = 0.0005
SLIP = 2.0  # bps


def atr14(h, l, c):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return pd.Series(tr).ewm(alpha=1.0 / ATR_P, adjust=False).mean().to_numpy()


async def main():
    m = joblib.load("reports/ml_tb_1m_v3.joblib")
    a = m["audit"]
    import importlib
    mtb = __import__("ml_train_tb")  # полный TICKERS (tcode кодирован его индексами)
    tk_full = mtb.TICKERS
    tcode_col = m["features"].index("tcode")
    tcode = np.round(a["X_oos"][:, tcode_col]).astype(int)
    tik = np.array([tk_full[t] for t in tcode])
    side = a["side_oos"]
    p = a["p_o"]; th = a["th_o"]
    ts = a["ts_oos"]

    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ticker, figi, lot FROM instruments "
            "WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37') ORDER BY ticker"),
            {"tk": tk_full})).fetchall()
    meta = {r.ticker: (r.figi, r.lot) for r in rows}
    print(f"loaded {len(meta)} tickers, keep threshold={th:.4f}")

    from app.services.signals import _load_candles as _lc
    agg = {"trades": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0,
           "net": 0.0, "dd": 0.0, "peak": 0.0, "horiz": 0}
    per_ticker = {}
    total_bars = 0
    for tkr in tk_full:
        if tkr not in meta:
            print(f"  {tkr}: NOT FOUND"); continue
        figi, lot = meta[tkr]
        async with SessionLocal() as db:
            c5 = await _lc(db, figi, 1, date_from=OOS_FROM, date_to=OOS_TO)
        if not c5:
            print(f"  {tkr}: no candles"); continue
        df = pd.DataFrame({"ts": [c.ts for c in c5],
                           "open": [float(c.open) for c in c5],
                           "high": [float(c.high) for c in c5],
                           "low": [float(c.low) for c in c5],
                           "close": [float(c.close) for c in c5]})
        df = df.sort_values("ts").reset_index(drop=True)
        atr = atr14(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy())
        cl = df["close"].to_numpy(); hi = df["high"].to_numpy(); lo = df["low"].to_numpy()
        tsn = df["ts"].values.astype("datetime64[ns]").astype("int64") // 10 ** 9
        total_bars += len(df)

        mask = (tik == tkr) & (ts >= tsn[0]) & (ts <= tsn[-1]) & (p >= th)
        sig = pd.DataFrame({"ts": ts[mask], "side": side[mask], "p": p[mask]})
        if len(sig) == 0:
            print(f"  {tkr}: 0 signals"); continue
        # максимум по паре (ts,side): одна сделка на бар -> сторона с большим p
        best = sig.sort_values("p").groupby("ts").tail(1)
        idx = np.searchsorted(tsn, best["ts"].to_numpy(), side="left")

        open_t = None
        n_tr = wn = ls = hz = 0
        g_w = g_l = 0.0
        tnet = 0.0
        for pos in range(len(best)):
            if open_t is not None and open_t + HOR * 60 > best.iloc[pos]["ts"]:
                continue  # занят
            i = idx[pos]
            if i >= len(cl):
                continue
            s = best.iloc[pos]["side"]
            a0 = atr[i]
            if not np.isfinite(a0) or a0 <= 0 or not np.isfinite(cl[i]):
                continue
            entry = cl[i]
            tp = entry + TP_ATR * a0 if s == "LONG" else entry - TP_ATR * a0
            sl = entry - SL_ATR * a0 if s == "LONG" else entry + SL_ATR * a0
            qty_shares = max(int(CAP_PER_TICKER / (entry * lot)) * lot, lot)
            # пройти вперёд до 60 баров
            ec = None; reason = None
            for j in range(i + 1, min(i + HOR + 1, len(cl))):
                if s == "LONG":
                    if hi[j] >= tp: ec, reason = tp, "TP"; break
                    if lo[j] <= sl: ec, reason = sl, "SL"; break
                else:
                    if lo[j] <= tp: ec, reason = tp, "TP"; break
                    if hi[j] >= sl: ec, reason = sl, "SL"; break
            if ec is None:
                ec = cl[min(i + HOR, len(cl) - 1)]
                reason = "HORIZON"
                hz += 1
            pnl = (ec - entry) * qty_shares if s == "LONG" else (entry - ec) * qty_shares
            slip = entry * (SLIP / 10000.0) * qty_shares
            pnl -= slip + entry * COMM * qty_shares * 1 + abs(ec) * COMM * qty_shares
            n_tr += 1
            tnet += pnl
            if pnl > 0:
                wn += 1; g_w += pnl
            else:
                ls += 1; g_l += abs(pnl)
            agg["net"] += pnl
            agg["peak"] = max(agg["peak"], agg["net"])
            agg["dd"] = max(agg["dd"], agg["peak"] - agg["net"])
            open_t = best.iloc[pos]["ts"]
        agg["trades"] += n_tr; agg["wins"] += wn; agg["losses"] += ls; agg["horiz"] += hz
        agg["gross_win"] += g_w; agg["gross_loss"] += g_l
        per_ticker[tkr] = {"trades": n_tr, "wins": wn, "losses": ls, "net": round(tnet, 2)}
        print(f"  {tkr}: {n_tr} trades (W{wn}/L{ls}/H{hz})", flush=True)

    out = {
        "model": "ml_tb_1m_v3", "period": [OOS_FROM.isoformat(), OOS_TO.isoformat()],
        "tickers": len(meta), "bars": total_bars,
        "trades": agg["trades"], "wins": agg["wins"], "losses": agg["losses"],
        "horizon": agg["horiz"],
        "gross_win": round(agg["gross_win"], 2), "gross_loss": round(agg["gross_loss"], 2),
        "net": round(agg["net"], 2),
        "pf": round(agg["gross_win"] / agg["gross_loss"], 2) if agg["gross_loss"] else None,
        "wr": round(agg["wins"] / max(1, agg["wins"] + agg["losses"]), 4),
        "avg_win": round(agg["gross_win"] / max(1, agg["wins"]), 2),
        "avg_loss": round(agg["gross_loss"] / max(1, agg["losses"]), 2),
        "max_dd": round(agg["dd"], 2),
    }
    print("\n=== Вариант B: прямые 1m-входы (OOS) ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    with open("reports/ml_tb_sim_B.json", "w") as f:
        json.dump({"direct_1m": out}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
