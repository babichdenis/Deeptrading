#!/usr/bin/env python3
"""Быстрый тест: направление RSI + фильтр объёма, с разрезом по режимам и MOEX.

Без движка: 5м бары, сигнал rsi_signal (направление), вход при rvol>=thr,
SL/TP = k*ATR(14), выход по SL/TP или через horizon баров.
Замеряем: trades/WR/net/PF/avg, разрез по режимам (H1-детектор) и MOEX (5м).

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/fast_rsi_vol_test.py --from 2026-09-01 --to 2026-09-08 --vol-thr 0.6 --sl 4.0 --rr 4.0
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import resample
from app.engine.models import Side
from app.engine import ensemble_v2 as ev
from app.engine.regime_strategies import detect_regime

IMOEX_FIGI = "BBG00KDWPPW2"
COMM = 0.0005  # комиссия 0.05% на сторону


def _blank():
    return {"n": 0, "w": 0, "l": 0, "gw": 0.0, "gl": 0.0}


def _add(t, pnl, win):
    t["n"] += 1
    if win:
        t["w"] += 1
        t["gw"] += pnl
    else:
        t["l"] += 1
        t["gl"] += pnl


def _show(name, t):
    n = t["n"]
    if not n:
        return "%s: 0" % name
    pf = (t["gw"] / abs(t["gl"])) if t["gl"] else float("inf")
    return "%-12s N=%-5d WR=%.1f%% Net=%+.1f%% PF=%.2f (W%+.1f/L%+.1f)" % (
        name, n, t["w"] / n * 100, t["gw"] + t["gl"], pf, t["gw"], t["gl"])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default="2026-09-01")
    ap.add_argument("--to", dest="dto", default="2026-09-08")
    ap.add_argument("--vol-thr", type=float, default=0.6)
    ap.add_argument("--sl", type=float, default=4.0)
    ap.add_argument("--rr", type=float, default=4.0)
    ap.add_argument("--horizon", type=int, default=48)
    args = ap.parse_args()
    f = datetime.fromisoformat(args.dfrom + "T00:00:00+00:00")
    t = datetime.fromisoformat(args.dto + "T23:59:00+00:00")

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier='eligible'
            AND NOT (i.ticker='T' AND i.figi='BBG000BSJK37') ORDER BY i.ticker
        """))).fetchall()
        meta = []
        for r in rows:
            if not any(r[1] == m[1] for m in meta):
                meta.append((r[0], r[1]))

        ic = await _lc(db, IMOEX_FIGI, 1, date_from=f, date_to=t)
        idx_dir = {}
        if ic:
            i5 = resample(ic, 300)
            for j in range(1, len(i5)):
                idx_dir[i5[j].ts] = 1 if float(i5[j].close) >= float(i5[j - 1].close) else -1
        print("IMOEX 5m bars:", len(idx_dir), "| tickers:", len(meta))

        tot = _blank()
        by_reg = defaultdict(_blank)
        by_idx = defaultdict(_blank)
        by_vol = defaultdict(_blank)
        per_t = defaultdict(_blank)
        for figi, tkr in meta:
            c1 = await _lc(db, figi, 1, date_from=f, date_to=t)
            if not c1 or len(c1) < 1500:
                continue
            c5 = resample(c1, 300)
            n = len(c5)
            if n < 260 + args.horizon:
                continue
            h1 = resample(c1, 3600)
            regime_by_ts = {}
            for k in range(2, len(h1)):
                try:
                    regime_by_ts[h1[k].ts] = detect_regime(h1[:k + 1]).regime
                except Exception:
                    pass
            closes = [float(x.close) for x in c5]
            highs = [float(x.high) for x in c5]
            lows = [float(x.low) for x in c5]
            vols = [float(x.volume or 0.0) for x in c5]
            atr = ev.atr_series(c5, 14) if hasattr(ev, "atr_series") else None
            if atr is None:
                atr = []
                tr = 0.0
                for k in range(1, n):
                    tr = max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]), abs(lows[k] - closes[k - 1]))
                    atr.append(tr)
            i = 260
            last_reg = ""
            while i < n - 2:
                ts = c5[i].ts
                if ts in regime_by_ts:
                    last_reg = regime_by_ts[ts]
                rvol = 1.0
                vsum = sum(vols[i - 20:i])
                if vsum > 0:
                    rvol = vols[i] / (vsum / 20.0)
                s = ev.rsi_signal(c5[:i + 1])
                if not s or rvol < args.vol_thr:
                    i += 1
                    continue
                a = atr[i - 1] if i - 1 < len(atr) else 0.0
                if a <= 0:
                    i += 1
                    continue
                entry = closes[i]
                if s == Side.BUY:
                    sl = entry - args.sl * a
                    tp = entry + args.sl * a * args.rr
                else:
                    sl = entry + args.sl * a
                    tp = entry - args.sl * a * args.rr
                exit_px = None
                j = i + 1
                end = min(n, i + 1 + args.horizon)
                while j < end:
                    if s == Side.BUY:
                        if lows[j] <= sl:
                            exit_px = sl
                            break
                        if highs[j] >= tp:
                            exit_px = tp
                            break
                    else:
                        if highs[j] >= sl:
                            exit_px = sl
                            break
                        if lows[j] <= tp:
                            exit_px = tp
                            break
                    j += 1
                if exit_px is None:
                    exit_px = closes[min(j, n - 1)]
                gross = (exit_px - entry) / entry if s == Side.BUY else (entry - exit_px) / entry
                pnl = (gross - 2 * COMM) * 100.0
                win = pnl > 0
                idir = idx_dir.get(ts, 0)
                ik = "idx_up" if idir > 0 else ("idx_dn" if idir < 0 else "idx_na")
                vk = "low<1" if rvol < 1 else ("mid1-2" if rvol < 2 else "high>2")
                _add(tot, pnl, win)
                _add(by_reg[last_reg or "?"], pnl, win)
                _add(by_idx[ik], pnl, win)
                _add(by_vol[vk], pnl, win)
                _add(per_t[tkr], pnl, win)
                i = j + 1

    print("=" * 90)
    print("RSI-DIR + VOL>=%.2f | SL=%.1fATR RR=%.1f | %s..%s" % (args.vol_thr, args.sl, args.rr, args.dfrom, args.dto))
    print("=" * 90)
    print(_show("ИТОГО", tot))
    print("-- по режимам --")
    for k in sorted(by_reg):
        print("  " + _show(k, by_reg[k]))
    print("-- по MOEX (5м) --")
    for k in ("idx_up", "idx_dn", "idx_na"):
        if k in by_idx:
            print("  " + _show(k, by_idx[k]))
    print("-- по объёму --")
    for k in ("low<1", "mid1-2", "high>2"):
        if k in by_vol:
            print("  " + _show(k, by_vol[k]))
    print("-- по тикерам (топ-8) --")
    for tkr, tt in sorted(per_t.items(), key=lambda x: -(x[1]["gw"] + x[1]["gl"]))[:8]:
        print("  " + _show(tkr, tt))


if __name__ == "__main__":
    asyncio.run(main())
