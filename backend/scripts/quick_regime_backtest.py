#!/usr/bin/env python3
"""Быстрый бэктест новой системы regime_strategies (3 стратегии + exit-менеджер).

Вход: сигнал на закрытии бара i → исполнение на open бара i+1 (без look-ahead).
Выход: ExitManager (SL/TP/трейлинг/time/regime) + сигнальные выходы стратегий.
Изолированные 10K/тикер, комиссия 0.05% + слиппедж 2 bps.

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/quick_regime_backtest.py --from 2026-08-15 --to 2026-09-12
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
from app.engine.models import Side
from app.engine.regime_strategies import (
    RegimeFilteredStrategies, detect_regime, _atr_last,
)

COMM = 0.0005
SLIP = 0.0002


def _fill(px, side):
    return px * (1 + SLIP) if side == Side.BUY else px * (1 - SLIP)


def run_ticker(candles, lot, params):
    strat = RegimeFilteredStrategies(params)
    pos = None
    trades = []
    n = len(candles)
    i = strat.warmup_bars()
    while i < n - 1:
        window = candles[:i + 1]
        regime = detect_regime(window)
        c = candles[i]

        if pos is not None:
            dec = strat.exit_manager.check_exit(pos, c, i, regime, window)
            if not dec.exit:
                for s in (strat.trend_strategy, strat.mr_strategy, strat.breakout_strategy):
                    if s.strategy_id == pos.strategy_id:
                        e = s.check_exit_signal(window, pos)
                        if e and e.exit:
                            dec = e
            if dec.exit and dec.price:
                ex = _fill(float(dec.price), Side.SELL if pos.side == Side.BUY else Side.BUY)
                qty = pos.qty
                gross = (ex - pos.entry_price) * qty if pos.side == Side.BUY else (pos.entry_price - ex) * qty
                fees = (pos.entry_price + ex) * qty * COMM
                trades.append({"net": gross - fees, "reason": dec.reason.value if dec.reason else "?",
                               "side": "LONG" if pos.side == Side.BUY else "SHORT"})
                pos = None

        if pos is None:
            sigs = strat.on_bar(window)
            if sigs:
                sig = sigs[0]
                nxt = candles[i + 1]
                entry = _fill(float(nxt.open), sig.side)
                atr = _atr_last(window, 14)
                budget = 10000.0
                lot_cost = entry * lot
                qty = max(int(budget / lot_cost) * lot, lot) if lot_cost > 0 else lot
                p = strat.exit_manager.create_position(sig, entry, i + 1, atr, regime.regime)
                p.qty = qty  # динамически
                pos = p
        i += 1

    if pos is not None and n > 1:
        ex = float(candles[-1].close)
        qty = pos.qty
        gross = (ex - pos.entry_price) * qty if pos.side == Side.BUY else (pos.entry_price - ex) * qty
        fees = (pos.entry_price + ex) * qty * COMM
        trades.append({"net": gross - fees, "reason": "end_of_data",
                       "side": "LONG" if pos.side == Side.BUY else "SHORT"})
    return trades


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default="2026-08-15")
    ap.add_argument("--to", dest="dto", default="2026-09-12")
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()
    f = datetime.fromisoformat(args.dfrom + "T00:00:00+00:00")
    t = datetime.fromisoformat(args.dto + "T23:59:00+00:00")
    only = [x.strip() for x in args.tickers.split(",") if x.strip()] or None

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier='eligible'
            AND NOT (i.ticker='T' AND i.figi='BBG000BSJK37') ORDER BY i.ticker
        """))).fetchall()
        meta = {}
        for r in rows:
            if r[1] in meta:
                continue
            if only and r[1] not in only:
                continue
            meta[r[1]] = (r[0], int(r[2]) if r[2] else 1)

        tot = defaultdict(float)
        by_side = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0})
        by_reason = defaultdict(lambda: {"n": 0, "net": 0.0})
        by_tkr = defaultdict(lambda: {"n": 0, "net": 0.0})
        for tkr, (figi, lot) in meta.items():
            c = await _lc(db, figi, 1, date_from=f, date_to=t)
            if not c or len(c) < 250:
                continue
            tr = run_ticker(c, lot, {})
            for x in tr:
                net = x["net"]
                tot["n"] += 1
                tot["net"] += net
                if net > 0:
                    tot["w"] += 1; tot["gw"] += net
                else:
                    tot["gl"] += net
                by_side[x["side"]]["n"] += 1
                by_side[x["side"]]["net"] += net
                by_side[x["side"]]["w"] += 1 if net > 0 else 0
                by_reason[x["reason"]]["n"] += 1
                by_reason[x["reason"]]["net"] += net
                by_tkr[tkr]["n"] += 1
                by_tkr[tkr]["net"] += net

    n = int(tot["n"])
    wr = tot["w"] / n * 100 if n else 0
    pf = tot["gw"] / abs(tot["gl"]) if tot["gl"] else 0
    print("=" * 90)
    print("QUICK REGIME BACKTEST %s .. %s (10K/тикер, вход next-open, comm+slip)" % (args.dfrom, args.dto))
    print("=" * 90)
    print("OVERALL: trades=%d net=%+.0f WR=%.1f%% PF=%.2f (gw=%+.0f gl=%+.0f)" % (
        n, tot["net"], wr, pf, tot["gw"], tot["gl"]))
    print("\nПо сторонам:")
    for k, b in sorted(by_side.items()):
        w = b["w"] / b["n"] * 100 if b["n"] else 0
        print("  %-6s N=%4d net=%+9.0f WR=%.1f%%" % (k, b["n"], b["net"], w))
    print("\nПо причинам выхода:")
    for k, b in sorted(by_reason.items(), key=lambda kv: -kv[1]["net"]):
        print("  %-16s N=%4d net=%+9.0f" % (k, b["n"], b["net"]))
    print("\nТоп/анти-тикеры:")
    for tkr, b in sorted(by_tkr.items(), key=lambda kv: -kv[1]["net"])[:6]:
        print("  %-7s N=%3d net=%+8.0f" % (tkr, b["n"], b["net"]))
    for tkr, b in sorted(by_tkr.items(), key=lambda kv: kv[1]["net"])[:4]:
        print("  %-7s N=%3d net=%+8.0f" % (tkr, b["n"], b["net"]))


if __name__ == "__main__":
    asyncio.run(main())
