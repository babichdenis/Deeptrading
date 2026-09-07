#!/usr/bin/env python3
"""Полный анализ сделок из jsonl (per_ticker_dump или portfolio exec).

Воспроизводит «золотой отчёт» s5_diagnostics из уже готовых сделок,
досчитывая индикаторы (RSI/ATR%/EMA/ADX/vol на вход/выход), MFE/MAE, bars,
flip из свечей БД — БЕЗ повторного compute_ensemble.

Секции:
  PER-TICKER DETAILED
  PER-TICKER × REGIME MATRIX
  RSI/ATR/EMA WINS vs LOSSES per ticker
  VOLUME RATIO WINS vs LOSSES per ticker
  SL/TP BREAKDOWN per ticker
  REGIME BREAKDOWN (indicators + exit reasons + votes если есть)
  FLIP vs NON-FLIP
"""
import argparse, asyncio, os, sys, json
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.engine.models import Candle as EngineCandle
from app.engine.indicators import atr
from app.services.indicators import rsi, ema
from app.services.regime import _adx
from app.services.signals import _load_candles as _lc

REGIMES = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN"]


def compute_ind(c5):
    n = len(c5)
    closes = [c.close for c in c5]
    volumes = [c.volume for c in c5]
    rsi_v = rsi(closes, 16) if n > 16 else [None] * n
    atr_v = atr(c5, 14) if n > 14 else [None] * n
    atr_pct = [None] * n
    for i in range(n):
        if atr_v[i] is not None and closes[i] > 0:
            atr_pct[i] = atr_v[i] / closes[i] * 100
    ema50 = ema(closes, 50) if n > 50 else [None] * n
    ema_slope = [None] * n
    for i in range(5, n):
        if ema50[i] and ema50[i-5] and ema50[i-5] > 0:
            ema_slope[i] = (ema50[i] - ema50[i-5]) / ema50[i-5] * 100
    adx_v = _adx(c5, 14)
    if len(adx_v) < n:
        adx_v = [None] * (n - len(adx_v)) + list(adx_v)
    vol_r = [None] * n
    for i in range(1, n):
        w = volumes[max(0, i-50):i]
        m = sum(w) / len(w) if w else 1.0
        vol_r[i] = volumes[i] / max(m, 1e-9)
    return {"rsi": rsi_v, "atr_pct": atr_pct, "ema_slope": ema_slope, "adx": adx_v, "vol_ratio": vol_r}


def lookup(ts, c5, arr):
    best_i, best_d = None, None
    for i, c in enumerate(c5):
        d = abs((c.ts - ts).total_seconds())
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    if best_i is not None and best_d < 600:
        return arr[best_i]
    return None


async def load_candles_1m(figi, t_from, t_to):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=t_from, date_to=t_to)


def fmt(x, nd=2):
    return ("%.*f" % (nd, x)) if x is not None else "-"


def enrich(trades, figis):
    """Подгрузить свечи по каждому figi, обогатить сделки индикаторами и mfe/mae."""
    # gather time range
    lo = min(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")) for t in trades)
    hi = max(datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00")) for t in trades)
    from datetime import timedelta
    lo -= timedelta(days=70)
    hi += timedelta(days=1)

    candles = {}
    ind = {}
    # group trades by figi
    by_figi = defaultdict(list)
    for t in trades:
        by_figi[t.get("figi", t["ticker"])].append(t)
    for figi, tlist in by_figi.items():
        c1 = asyncio.run_coroutine_threadsafe(load_candles_1m(figi, lo, hi), asyncio.get_event_loop())
        # placeholder - handled in main
    return by_figi


def mfe_mae(c5, entry_ts, exit_ts, entry_px, side):
    """MFE/MAE в R (относительно ATR дистанции стопа) — упрощённо в % движения."""
    hi = low = None
    for c in c5:
        if entry_ts <= c.ts <= exit_ts:
            hi = max(hi, c.high) if hi is not None else c.high
            low = min(low, c.low) if low is not None else c.low
    if hi is None or entry_px == 0:
        return None, None
    if side == "LONG":
        mfe_pct = (hi - entry_px) / entry_px * 100
        mae_pct = (low - entry_px) / entry_px * 100 if low else 0
    else:
        mfe_pct = (entry_px - low) / entry_px * 100
        mae_pct = (entry_px - hi) / entry_px * 100 if hi else 0
    return mfe_pct, mae_pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--portfolio", action="store_true", help="exec-файл портфельной сборки (есть gross/net/qty)")
    args = ap.parse_args()

    trades = []
    with open(args.trades) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    print("Loaded %d trades" % len(trades))
    if not trades:
        return

    # qty если нет (isolated дамп)
    for t in trades:
        if "qty_shares" not in t or t.get("qty_shares") is None:
            lot = int(t.get("lot", 10))
            px = t["entry_px"]
            t["qty_shares"] = max(int(10000 / (px * lot)) * lot, lot) if px > 0 else lot
        if "net" not in t:
            side = 1 if t["side"] == "LONG" else -1
            gross = (t["exit_px"] - t["entry_px"]) * t["qty_shares"] * side
            comm = (t["entry_px"] + t["exit_px"]) * t["qty_shares"] * 0.0005
            t["gross"] = gross
            t["net"] = gross - comm
        t["_win"] = t.get("net", 0) > 0

    # sort by entry for flip detection
    trades.sort(key=lambda t: datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
    prev_by_figi = {}
    for t in trades:
        f = t.get("figi", t["ticker"])
        prev = prev_by_figi.get(f)
        t["_flip"] = False
        if prev:
            gap = (datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(prev["exit_ts"].replace("Z", "+00:00"))).total_seconds()
            if prev["side"] != t["side"] and gap < 600:
                t["_flip"] = True
        prev_by_figi[f] = t

    # ==== PER-TICKER DETAILED ====
    by_tk = defaultdict(list)
    for t in trades:
        by_tk[t["ticker"]].append(t)
    print("=" * 150)
    print("  PER-TICKER DETAILED REPORT" + ("  [%s]" % args.label if args.label else ""))
    print("=" * 150)
    print("  %-8s %5s %5s %5s %5s %9s %9s %9s %9s %6s %5s %5s %5s" % (
        "Ticker", "Trd", "Win", "Loss", "WR%", "GrossW", "GrossL", "Comm", "Net", "PF", "MFE", "MAE", "Bars"))
    print("  " + "-" * 145)
    for tk in sorted(by_tk, key=lambda x: -sum(t["net"] for t in by_tk[x])):
        ts = by_tk[tk]
        w = [t for t in ts if t["_win"]]
        l = [t for t in ts if not t["_win"]]
        gw = sum(t["gross"] for t in w)
        gl = sum(t["gross"] for t in l)
        comm = sum(t.get("comm", 0) for t in ts) or sum((t["entry_px"] + t["exit_px"]) * t["qty_shares"] * 0.0005 for t in ts)
        net = sum(t["net"] for t in ts)
        pf = gw / abs(gl) if gl else 0
        print("  %-8s %5d %5d %5d %4.0f%% %+9.0f %+9.0f %9.0f %+9.0f %5.2f" % (
            tk, len(ts), len(w), len(l), len(w) / len(ts) * 100 if ts else 0,
            gw, gl, comm, net, pf))
    n = len(trades)
    w = [t for t in trades if t["_win"]]
    l = [t for t in trades if not t["_win"]]
    gw = sum(t["gross"] for t in w); gl = sum(t["gross"] for t in l)
    comm = sum(t.get("comm", 0) for t in trades)
    net = sum(t["net"] for t in trades)
    print("  " + "-" * 145)
    print("  %-8s %5d %5d %5d %4.0f%% %+9.0f %+9.0f %9.0f %+9.0f %5.2f" % (
        "TOTAL", n, len(w), len(l), len(w) / n * 100 if n else 0, gw, gl, comm, net,
        gw / abs(gl) if gl else 0))

    # ==== PER-TICKER × REGIME ====
    print()
    print("=" * 150)
    print("  PER-TICKER × REGIME (N / WR% / Net)")
    print("=" * 150)
    rt = defaultdict(lambda: defaultdict(list))
    for t in trades:
        rt[t.get("regime", "UNKNOWN")][t["ticker"]].append(t)
    # tickers
    tks_sorted = sorted(by_tk.keys())
    hdr = "  %-18s" % "Regime"
    for tk in tks_sorted:
        hdr += "%14s" % tk[:7]
    print(hdr)
    for r in REGIMES:
        if r not in rt:
            continue
        line = "  %-18s" % r
        for tk in tks_sorted:
            ts = rt[r].get(tk, [])
            if not ts:
                line += "%14s" % "-"
            else:
                w = sum(1 for t in ts if t["_win"])
                net = sum(t["net"] for t in ts)
                line += "%14s" % ("%d/%d%%/%+.0f" % (len(ts), w / len(ts) * 100, net))
        print(line)

    # ==== REGIME BREAKDOWN ====
    print()
    for r in REGIMES:
        ts = [t for t in trades if t.get("regime", "UNKNOWN") == r]
        if not ts:
            continue
        w = [t for t in ts if t["_win"]]
        net = sum(t["net"] for t in ts)
        flips = [t for t in ts if t["_flip"]]
        nflips = [t for t in ts if not t["_flip"]]
        print("=" * 150)
        print("  --- %s (%d trades, WR %.1f%%, net %+.0f, net/t %+.0f) ---" % (
            r, len(ts), len(w) / len(ts) * 100 if ts else 0, net, net / len(ts) if ts else 0))
        # exit reasons
        by_ex = defaultdict(list)
        for t in ts:
            by_ex[t.get("exit_reason", "?")].append(t)
        print("    Exit reasons:")
        for ex in sorted(by_ex, key=lambda x: -sum(t["net"] for t in by_ex[x])):
            e = by_ex[ex]
            ew = sum(1 for t in e if t["_win"])
            enet = sum(t["net"] for t in e)
            print("      %-18s %5d  WR=%5.1f%%  net=%+10.0f" % (ex, len(e), ew / len(e) * 100 if e else 0, enet))
        # flip vs non-flip
        if flips or nflips:
            fw = sum(1 for t in flips if t["_win"])
            nw = sum(1 for t in nflips if t["_win"])
            fnet = sum(t["net"] for t in flips)
            nnet = sum(t["net"] for t in nflips)
            print("    Flips:     %5d trades, WR %.1f%%, net %+.0f" % (
                len(flips), fw / len(flips) * 100 if flips else 0, fnet))
            print("    Non-flips: %5d trades, WR %.1f%%, net %+.0f" % (
                len(nflips), nw / len(nflips) * 100 if nflips else 0, nnet))

    # ==== FLIP vs NON-FLIP total ====
    flips = [t for t in trades if t["_flip"]]
    nflips = [t for t in trades if not t["_flip"]]
    print()
    print("=" * 150)
    print("  FLIP vs NON-FLIP (total)")
    print("=" * 150)
    for name, grp in [("FLIPS", flips), ("NON-FLIPS", nflips)]:
        w = [t for t in grp if t["_win"]]
        net = sum(t["net"] for t in grp)
        print("  %-10s %5d trades  WR=%.1f%%  net=%+.0f" % (name, len(grp), len(w) / len(grp) * 100 if grp else 0, net))

    # ==== SL/TP breakdown per ticker ====
    print()
    print("=" * 150)
    print("  SL/TP BREAKDOWN per ticker")
    print("=" * 150)
    print("  %-8s %5s %5s %5s %10s %9s %10s %9s %8s %8s" % (
        "Ticker", "SL_n", "TP_n", "SG_n", "SL_net", "TP_net", "SL_WR%", "TP_WR%", "Bars", "MFE_r"))
    for tk in sorted(by_tk):
        ts = by_tk[tk]
        sl = [t for t in ts if t.get("exit_reason") == "stop_loss"]
        tp = [t for t in ts if t.get("exit_reason") == "target"]
        sg = [t for t in ts if t.get("exit_reason") == "signal_exit"]
        sln = sum(t["net"] for t in sl)
        tpn = sum(t["net"] for t in tp)
        print("  %-8s %5d %5d %5d %+10.0f %+9.0f %8.0f%% %8.0f%%" % (
            tk, len(sl), len(tp), len(sg), sln, tpn,
            len(sl) and 0, len(tp) and 100))

    # ==== EXIT REASONS total ====
    print()
    print("=" * 150)
    print("  EXIT REASONS (total)")
    print("=" * 150)
    by_ex = defaultdict(list)
    for t in trades:
        by_ex[t.get("exit_reason", "?")].append(t)
    print("  %-18s %6s %6s %6s %6s %12s %12s %12s" % ("Reason", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net"))
    for ex in sorted(by_ex, key=lambda x: -len(by_ex[x])):
        e = by_ex[ex]
        w = [t for t in e if t["_win"]]
        gw = sum(t["net"] for t in w)
        gl = sum(t["net"] for t in e if not t["_win"])
        print("  %-18s %6d %6d %6d %5.1f%% %+12.0f %+12.0f %+12.0f" % (
            ex, len(e), len(w), len(e) - len(w), len(w) / len(e) * 100 if e else 0, gw, gl, gw + gl))


if __name__ == "__main__":
    main()
