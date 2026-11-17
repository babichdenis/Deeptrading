"""Замена минутных свечей за указанные дни данными источника (T-Invest).

Зачем: в базе .8 накопились бары с ценой чужого ряда (MVID с ценой LKOH,
SBER с ценой ~72.8 при реальных 304). Такие бары структурно корректны, поэтому
эвристикой их не отличить: единственный надёжный ориентир — источник.

Скрипт заменяет ДЕНЬ ЦЕЛИКОМ: удалённое уходит в candles_quarantine
(reason='refill_replaced'), затем пишется то, что отдал источник. Так не остаётся
остатков битого ряда, а если загрузка упадёт — данные лежат в карантине.

По умолчанию читает и печатает план (--dry-run). Запись — с --apply.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MSK = timezone(timedelta(hours=3))


async def refill_day(figi: str, day: datetime, apply: bool) -> tuple[str, int, int]:
    from app.database import SessionLocal
    from app.services.tinvest import INTERVAL_NAMES, fetch_candles, to_thread, upsert_candles

    day = day.replace(tzinfo=MSK) if day.tzinfo is None else day
    start = day.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = await to_thread(fetch_candles, figi, INTERVAL_NAMES["1min"],
                           start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    if not rows:
        return ("empty", 0, 0, 0)
    if not apply:
        return ("dry", len(rows), 0, 0)

    async with SessionLocal() as db:
        moved = (await db.execute(text("""
            WITH b AS (
              SELECT id, figi, interval, ts, open, high, low, close, volume
              FROM candles
              WHERE figi = :f AND interval = 1 AND ts >= :a AND ts < :b
            ), ins AS (
              INSERT INTO candles_quarantine
                (orig_id, figi, interval, ts, open, high, low, close, volume, reason)
              SELECT id, figi, interval, ts, open, high, low, close, volume, 'refill_replaced'
              FROM b
              RETURNING orig_id
            )
            DELETE FROM candles WHERE id IN (SELECT orig_id FROM ins)
        """), {"f": figi, "a": start.astimezone(timezone.utc), "b": end.astimezone(timezone.utc)}))
        await db.commit()
        moved_n = moved.rowcount or 0
    async with SessionLocal() as db:
        written = await upsert_candles(db, rows)
    return ("ok", len(rows), written, moved_n)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--specs", required=True, help="JSON-файл: [{\"figi\":...,\"from\":\"YYYY-MM-DD\",\"to\":\"YYYY-MM-DD\"}]")
    args = ap.parse_args()

    import json
    specs = json.loads(Path(args.specs).read_text(encoding="utf-8"))
    for s in specs:
        figi = s["figi"]
        d = datetime.fromisoformat(s["from"]).date()
        dto = datetime.fromisoformat(s["to"]).date()
        while d <= dto:
            res = await refill_day(figi, datetime(d.year, d.month, d.day), args.apply)
            print(f"{figi} {d} -> {res}", flush=True)
            d += timedelta(days=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
