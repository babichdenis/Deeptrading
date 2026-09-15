#!/usr/bin/env python3
"""AI-gate tracker: достраивает исходы решений гейта (контрфакт +15/+30 мин и факт P&L).

Для каждой записи ai_decisions без outcome_ts (старше 31 мин) считает:
- px_entry / px_15m / px_30m по 1м свечам инструмента;
- контрфакт pnl_cf_30m = движение в сторону заявки × qty_shares − комиссии (0.05%×2);
- saved_rub (если контрфакт был бы убытком) / missed_rub (если прибылью);
- actual_pnl — фактический net_pnl сделки из sandbox_trades (если вход состоялся).

Запуск (на .3):
    .venv/bin/python3 scripts/ai_gate_tracker.py            # цикл раз в 60с
    .venv/bin/python3 scripts/ai_gate_tracker.py --once     # один прогон
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

import asyncpg

DSN = os.environ.get("TRACKER_DSN", "postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading")
COMM = 0.0005  # комиссия на сторону (как в конфиге бота)


async def _run_once(conn: asyncpg.Connection, days: int, verbose: bool = True) -> int:
    now = datetime.now(timezone.utc)
    rows = await conn.fetch(
        """SELECT id, ts, figi, ticker, side, qty, price
           FROM ai_decisions
           WHERE outcome_ts IS NULL AND ts <= $1 AND ts >= $2
           ORDER BY ts LIMIT 200""",
        now - timedelta(minutes=31), now - timedelta(days=days),
    )
    if verbose and rows:
        print(f"[tracker] к обработке: {len(rows)}")
    n_done = 0
    for r in rows:
        ts = r["ts"]
        figi = r["figi"]
        side = str(r["side"] or "").upper()
        qty_lots = int(r["qty"] or 0)
        if not figi or side not in ("BUY", "SELL"):
            await conn.execute("UPDATE ai_decisions SET outcome_ts=$1 WHERE id=$2", now, r["id"])
            continue
        lot = await conn.fetchval("SELECT lot FROM instruments WHERE figi=$1", figi) or 1
        qty_sh = max(1, qty_lots) * int(lot)
        candles = await conn.fetch(
            """SELECT ts, close FROM candles
               WHERE figi=$1 AND interval=1 AND ts >= $2 AND ts <= $3 ORDER BY ts""",
            figi, ts - timedelta(minutes=5), ts + timedelta(minutes=35),
        )
        if not candles:
            await conn.execute("UPDATE ai_decisions SET outcome_ts=$1 WHERE id=$2", now, r["id"])
            continue

        def _close_at(target: datetime):
            best = None
            for c in candles:
                if c["ts"] <= target:
                    best = float(c["close"])
                else:
                    break
            return best

        px_entry = _close_at(ts) or float(r["price"] or 0.0) or None
        px_15 = _close_at(ts + timedelta(minutes=15))
        px_30 = _close_at(ts + timedelta(minutes=30))
        pnl_cf = saved = missed = None
        if px_entry and px_30:
            direction = 1.0 if side == "BUY" else -1.0
            gross = (px_30 - px_entry) * qty_sh * direction
            costs = COMM * 2.0 * px_entry * qty_sh
            pnl_cf = round(gross - costs, 2)
            saved = round(-pnl_cf, 2) if pnl_cf < 0 else 0.0
            missed = round(pnl_cf, 2) if pnl_cf > 0 else 0.0
        actual = await conn.fetchval(
            """SELECT net_pnl FROM sandbox_trades
               WHERE figi=$1 AND entry_time >= $2 AND entry_time <= $3
                 AND test_name IS NULL
               ORDER BY abs(extract(epoch from (entry_time - $2))) LIMIT 1""",
            figi, ts - timedelta(minutes=2), ts + timedelta(minutes=5),
        )
        await conn.execute(
            """UPDATE ai_decisions
               SET outcome_ts=$1, px_entry=$2, px_15m=$3, px_30m=$4, pnl_cf_30m=$5,
                   saved_rub=$6, missed_rub=$7, actual_pnl=$8
               WHERE id=$9""",
            now, px_entry, px_15, px_30, pnl_cf, saved, missed,
            (float(actual) if actual is not None else None), r["id"],
        )
        n_done += 1
        if verbose:
            print(f"  {r['ticker']:6s} {side:4s} x{qty_lots} {ts:%H:%M} "
                  f"px {px_entry}→{px_30} cf={pnl_cf} saved={saved} missed={missed} actual={actual}")
    return n_done


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    conn = await asyncpg.connect(DSN)
    try:
        if args.once:
            n = await _run_once(conn, args.days)
            print(f"[tracker] done: {n}")
            return
        print(f"[tracker] start (interval={args.interval}s, days={args.days})")
        while True:
            try:
                await _run_once(conn, args.days)
            except Exception as e:
                print(f"[tracker] cycle error: {type(e).__name__}: {str(e)[:120]}")
            await asyncio.sleep(max(15.0, args.interval))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
