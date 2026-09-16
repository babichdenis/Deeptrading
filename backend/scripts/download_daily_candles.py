"""Скачать дневные бары (interval=24) с MOEX ISS в candles.

Использование:
  python scripts/download_daily_candles.py                # вселенная бота (universe)
  python scripts/download_daily_candles.py --all          # все TQBR-акции из instruments
  python scripts/download_daily_candles.py --tickers SBER,GAZP --days 500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def _tickers(args) -> list[tuple[str, str]]:
    from sqlalchemy import text
    from app.database import SessionLocal
    async with SessionLocal() as db:
        if args.tickers:
            rows = (await db.execute(text(
                "SELECT figi, ticker FROM instruments WHERE ticker = ANY(:ts)"),
                {"ts": [t.strip().upper() for t in args.tickers.split(",") if t.strip()]})).all()
        elif args.all:
            rows = (await db.execute(text(
                "SELECT figi, ticker FROM instruments WHERE class_code = 'TQBR' "
                "ORDER BY ticker"))).all()
        else:
            rows = (await db.execute(text(
                "SELECT figi, ticker FROM universe ORDER BY ticker"))).all()
    return [(str(r[0]), str(r[1])) for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="", help="список тикеров через запятую")
    ap.add_argument("--all", action="store_true", help="все TQBR из instruments")
    ap.add_argument("--days", type=int, default=400, help="глубина истории (дней)")
    ap.add_argument("--pause", type=float, default=0.25, help="пауза между запросами, сек")
    args = ap.parse_args()

    from app.bot.moex import sync_moex_daily
    items = await _tickers(args)
    print(f"тикеров: {len(items)} · глубина {args.days} дн.", flush=True)
    ok = fail = total = 0
    t0 = time.monotonic()
    for i, (figi, ticker) in enumerate(items, 1):
        try:
            n = await asyncio.to_thread(sync_moex_daily, figi, ticker, args.days)
            total += n
            ok += 1 if n else 0
            if i % 10 == 0 or n == 0:
                print(f"  [{i}/{len(items)}] {ticker}: {n} баров (всего {total})", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(items)}] {ticker}: ОШИБКА {type(e).__name__}: {str(e)[:80]}", flush=True)
        await asyncio.sleep(args.pause)
    dt = time.monotonic() - t0
    print(f"готово: тикеров ok={ok} fail={fail}, баров={total}, {dt:.0f}с", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
