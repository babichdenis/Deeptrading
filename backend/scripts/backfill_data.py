"""CLI: восполнение 1m-свечей. Пример:
  .venv/bin/python scripts/backfill_data.py --auto --days 35 --workers 6
  .venv/bin/python scripts/backfill_data.py --figis BBG004730N88:ALRS TCS00A108ZR8:DATA
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from app.database import SessionLocal
from app.services.data_backfill import run_backfill


def _resolve_figis(items: list[str]) -> list[tuple[str, str]]:
    out = []
    for it in items:
        if ":" in it:
            figi, ticker = it.split(":", 1)
            out.append((figi, ticker))
        else:
            out.append((it, ""))
    return out


async def _auto(lookback_days: int) -> list[tuple[str, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT figi, ticker FROM sandbox_trades "
            "WHERE entry_time >= :cut OR exit_time IS NULL"
        ), {"cut": cutoff})).fetchall()
    items = [(r[0], r[1] or "") for r in rows]
    need = [f for f, t in items if not t]
    if need:
        async with SessionLocal() as db:
            rows2 = (await db.execute(text(
                "SELECT figi, ticker FROM instruments WHERE figi = ANY(:ff) "
                "UNION SELECT figi, ticker FROM instrument_info WHERE figi = ANY(:ff)"
            ), {"ff": need})).fetchall()
        tmap = {r[0]: r[1] for r in rows2}
        items = [(f, tmap.get(f) or t) for f, t in items]
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--figis", nargs="*", default=None)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.figis:
        instruments = _resolve_figis(args.figis)
    else:
        instruments = asyncio.run(_auto(args.lookback))
    print(f"instruments: {len(instruments)}")
    if not instruments:
        print("нет инструментов")
        return
    rep = asyncio.run(run_backfill(instruments, days=args.days, workers=args.workers))
    print("REPORT:", rep)


if __name__ == "__main__":
    main()
