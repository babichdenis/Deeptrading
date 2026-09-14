#!/usr/bin/env python3
"""Мониторинг открытых позиций: цена vs SL/TP, защита прибыли.

Каждый цикл читает открытые сделки (sandbox_trades, exit_time IS NULL) и
последние 1м свечи из БД, считает P&L и дистанцию до SL/TP, пишет компактную
таблицу. При подходе цены к уровню ближе alert_pct — пишет ⚠ АЛЕРТ.

Смена уровней (вручную, из другого процесса):
    POST http://127.0.0.1:8000/api/v1/bot/positions/levels
    {"ticker": "SMLT", "sl": 304, "tp": 292}

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/watch_positions.py --interval 60
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal


async def _cycle(alert_pct: float) -> None:
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT t.id, t.ticker, t.side, t.qty, t.entry_price, t.stop_loss, t.take_profit,
                   t.leverage, (SELECT c.close FROM candles c
                                WHERE c.figi = t.figi AND c.interval = 1
                                ORDER BY c.ts DESC LIMIT 1) AS last_price
            FROM sandbox_trades t
            WHERE t.exit_time IS NULL
            ORDER BY t.entry_time DESC
        """))).fetchall()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if not rows:
        print(f"[{now}] открытых позиций нет", flush=True)
        return
    print(f"[{now}] открытых: {len(rows)}", flush=True)
    print(f"  {'тикер':<7}{'side':<5}{'qty':>4} {'вход':>9} {'цена':>9} {'P&L₽':>8} "
          f"{'SL':>10}{'до SL%':>8} {'TP':>10}{'до TP%':>8}", flush=True)
    alerts: list[str] = []
    for r in rows:
        (rid, tkr, side, qty, entry, sl, tp, lev, price) = r
        if price is None or entry is None:
            continue
        entry = float(entry)
        price = float(price)
        sl = float(sl) if sl is not None else None
        tp = float(tp) if tp is not None else None
        long_ = str(side).upper() in ("LONG", "BUY")
        pnl = (price - entry) * int(qty) * (1 if long_ else -1)
        d_sl = (abs(price - sl) / price * 100) if sl else None
        d_tp = (abs(tp - price) / price * 100) if tp else None
        print(f"  {tkr:<7}{'▲L' if long_ else '▼S':<5}{int(qty):>4} {entry:>9.2f} {price:>9.2f} "
              f"{pnl:>+8.1f} {sl if sl else 0:>10.2f}{(d_sl if d_sl is not None else 0):>7.2f}% "
              f"{tp if tp else 0:>10.2f}{(d_tp if d_tp is not None else 0):>7.2f}%", flush=True)
        if d_tp is not None and d_tp <= alert_pct:
            alerts.append(f"⚠ {tkr}: цена {price:.2f} в {d_tp:.2f}% от TP {tp:.2f} (P&L {pnl:+.1f}₽)")
        if d_sl is not None and d_sl <= alert_pct:
            alerts.append(f"⚠ {tkr}: цена {price:.2f} в {d_sl:.2f}% от SL {sl:.2f} (P&L {pnl:+.1f}₽)")
    for a in alerts:
        print("  " + a, flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--alert-pct", type=float, default=0.5)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    while True:
        try:
            await _cycle(args.alert_pct)
        except Exception as e:
            print(f"[watch] ERROR {type(e).__name__}: {str(e)[:120]}", flush=True)
        if args.once:
            return
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
