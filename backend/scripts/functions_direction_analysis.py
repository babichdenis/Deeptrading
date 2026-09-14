#!/usr/bin/env python3
"""Быстрый анализ функций: угадывают ли направление и при каком объёме.

Для каждого 5м-бара считаем сигналы 10 функций (ensemble_v2) и смотрим
доходность вперёд (1/2/3 бара 5м). Агрегируем по функции:
  N, hit% (направление верное), avg forward return, и по корзинам rvol.

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/functions_direction_analysis.py --from 2026-08-15 --to 2026-09-12
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import resample
from app.engine.models import Side
from app.engine import ensemble_v2 as ev

FUNCS = [
    ("micro_breakout", lambda c: ev.micro_breakout(c, 5, 3.0)),
    ("ema_signal", ev.ema_signal),
    ("macd_signal", ev.macd_signal),
    ("rsi_signal", ev.rsi_signal),
    ("donchian", ev.donchian_m5),
    ("pullback", ev.pullback_m5),
    ("range", ev.range_breakout_m5),
    ("volume", ev.volume_breakout_m5),
    ("bollinger", ev.bollinger_m5),
    ("atr_breakout", ev.atr_breakout_m5),
]
HORIZON = 3  # баров 5м вперёд


def _blank():
    return {"n": 0, "hit": 0, "ret": 0.0}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default="2026-08-15")
    ap.add_argument("--to", dest="dto", default="2026-09-12")
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
            if any(r[1] == m[1] for m in meta):
                continue
            meta.append((r[0], r[1]))

        # IMOEX 5м направление
        _IMOEX_FIGI = "BBG00KDWPPW2"  # IMOEX (индекса нет в instruments, свечи есть)
        idx_dir: dict = {}
        ic = await _lc(db, _IMOEX_FIGI, 1, date_from=f, date_to=t)
        if ic:
            i5 = resample(ic, 300)
            for j in range(1, len(i5)):
                idx_dir[i5[j].ts] = 1 if float(i5[j].close) >= float(i5[j - 1].close) else -1
        print("IMOEX 5m bars:", len(idx_dir))

        stat = {name: _blank() for name, _ in FUNCS}
        by_vol = {name: defaultdict(_blank) for name, _ in FUNCS}
        by_idx = {name: defaultdict(_blank) for name, _ in FUNCS}
        for figi, tkr in meta:
            c1 = await _lc(db, figi, 1, date_from=f, date_to=t)
            if not c1 or len(c1) < 2000:
                continue
            c5 = resample(c1, 300)
            n = len(c5)
            if n < 260 + HORIZON:
                continue
            closes = [float(x.close) for x in c5]
            vols = [float(x.volume or 0.0) for x in c5]
            for i in range(260, n - HORIZON):
                w = c5[:i + 1]
                fut = closes[i + HORIZON]
                cur = closes[i]
                if cur <= 0:
                    continue
                fwd = (fut - cur) / cur * 100
                vs = sum(vols[max(0, i - 20):i]) / max(1, len(vols[max(0, i - 20):i]))
                rvol = (vols[i] / vs) if vs > 0 else 1.0
                bucket = "low<1" if rvol < 1 else ("mid1-2" if rvol < 2 else "high>2")
                for name, fn in FUNCS:
                    try:
                        s = fn(w)
                    except Exception:
                        s = None
                    if not s:
                        continue
                    ok = (s == Side.BUY and fwd > 0) or (s == Side.SELL and fwd < 0)
                    signed = fwd if s == Side.BUY else -fwd
                    _id = idx_dir.get(c5[i].ts, 0)
                    _ik = "idx_up" if _id > 0 else ("idx_dn" if _id < 0 else "idx_na")
                    for tgt in (stat[name], by_vol[name][bucket], by_idx[name][_ik]):
                        tgt["n"] += 1
                        tgt["hit"] += 1 if ok else 0
                        tgt["ret"] += signed

    print("=" * 100)
    print("НАПРАВЛЕНИЕ ФУНКЦИЙ: горизонт %d баров 5м, %s..%s" % (HORIZON, args.dfrom, args.dto))
    print("=" * 100)
    print("%-16s %7s %7s %10s | %-28s" % ("функция", "N", "hit%", "avg ret%", "по объёму (N/hit%/avg)"))
    for name, _ in FUNCS:
        b = stat[name]
        if not b["n"]:
            print("%-16s %7d" % (name, 0))
            continue
        hit = b["hit"] / b["n"] * 100
        avg = b["ret"] / b["n"]
        vols = []
        for bk in ("low<1", "mid1-2", "high>2"):
            vb = by_vol[name].get(bk)
            if vb and vb["n"]:
                vols.append("%s:%d/%.0f%%/%+.2f" % (bk, vb["n"], vb["hit"] / vb["n"] * 100, vb["ret"] / vb["n"]))
        idxs = []
        for ik in ("idx_up", "idx_dn"):
            ib = by_idx[name].get(ik)
            if ib and ib["n"]:
                idxs.append("%s:%d/%.0f%%/%+.2f" % (ik, ib["n"], ib["hit"] / ib["n"] * 100, ib["ret"] / ib["n"]))
        print("%-16s %7d %6.1f%% %+9.3f%% | %s | %s" % (name, b["n"], hit, avg, "  ".join(vols), "  ".join(idxs)))


if __name__ == "__main__":
    asyncio.run(main())
