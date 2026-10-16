"""Докачка свежих 1m-свечей после 2026-10-08 для 24-ного universe (holdout).

Прямая докачка через T-Invest SDK → upsert в candles (не через candlehub ensure,
который ищет дыры только МЕЖДУ первой/последней свечой ряда). Тикеры берутся
из figis_full.txt (24 полных макета с 2024-01). Диапазон = [last_db_ts, now].
"""
from __future__ import annotations
import sys, asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.models.candle import Candle as DBCandle
from app.services.tinvest import fetch_candles, INTERVAL_NAMES, upsert_candles

IV = INTERVAL_NAMES["1min"]


def _load_targets() -> list[tuple[str, str]]:
    p = Path("/private/var/folders/xd/mbm9tg015tz3cnm8szvdw8000000gp/T/opencode/figis_full.txt")
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        figi, ticker = (line.split(":", 1) + [""])[:2]
        out.append((figi, ticker))
    return out


async def main() -> None:
    targets = _load_targets()
    now = datetime.now(timezone.utc)
    total_committed = 0
    async with SessionLocal() as db:
        for figi, ticker in targets:
            last = (await db.execute(
                select(DBCandle.ts).where(
                    DBCandle.figi == figi, DBCandle.interval == 1
                ).order_by(DBCandle.ts.desc()).limit(1)
            )).scalar()
            start = last if last else (now - timedelta(days=6))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start >= now - timedelta(minutes=2):
                print(f"{ticker:6} up-to-date (last={start})", flush=True)
                continue
            try:
                rows = await asyncio.to_thread(fetch_candles, figi, IV, start, now)
            except Exception as exc:
                print(f"{ticker:6} FETCH_ERR {type(exc).__name__}: {exc}", flush=True)
                continue
            if not rows:
                print(f"{ticker:6} 0 rows from {start}", flush=True)
                continue
            committed = await upsert_candles(db, rows)
            total_committed += committed
            mx = max(r["ts"] for r in rows)
            print(f"{ticker:6} committed={committed} max={mx}", flush=True)
    print(f"DONE total_committed={total_committed}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
