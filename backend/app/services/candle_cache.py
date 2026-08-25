from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candle import Candle
from app.services.tinvest import (
    INTERVAL_NAMES,
    fetch_candles,
    to_thread,
    upsert_candles,
)

DEFAULT_DAYS = 120

_INTERVAL_STEP = {
    "1min": timedelta(minutes=2),
    "5min": timedelta(minutes=10),
    "10min": timedelta(minutes=20),
    "15min": timedelta(minutes=30),
    "hour": timedelta(hours=2),
    "2h": timedelta(hours=4),
    "4h": timedelta(hours=8),
    "day": timedelta(days=2),
    "week": timedelta(days=15),
    "month": timedelta(days=45),
}


async def candle_coverage(
    db: AsyncSession, figi: str, interval_value: int
) -> tuple[datetime | None, datetime | None, int]:
    result = await db.execute(
        select(func.min(Candle.ts), func.max(Candle.ts), func.count()).where(
            Candle.figi == figi, Candle.interval == interval_value
        )
    )
    cov_min, cov_max, count = result.one()
    return cov_min, cov_max, int(count)


def missing_ranges(
    cov_min: datetime | None,
    cov_max: datetime | None,
    want_from: datetime,
    want_to: datetime,
    step: timedelta,
) -> list[tuple[datetime, datetime]]:
    if cov_max is None or cov_min is None:
        return [(want_from, want_to)]
    ranges: list[tuple[datetime, datetime]] = []
    if cov_max < want_to - step:
        ranges.append((cov_max + step, want_to))
    if cov_min > want_from + step:
        ranges.append((want_from, cov_min - step))
    return ranges


async def ensure_candles(
    db: AsyncSession,
    figi: str,
    interval_name: str,
    days: int = DEFAULT_DAYS,
    range_from: datetime | None = None,
    range_to: datetime | None = None,
) -> dict:
    interval = INTERVAL_NAMES.get(interval_name)
    if interval is None:
        raise ValueError(f"unknown interval: {interval_name}")
    interval_value = int(getattr(interval, "value", interval))

    now = datetime.now(timezone.utc)
    want_to = range_to or (now - timedelta(minutes=10))
    want_from = range_from or (want_to - timedelta(days=days))
    if want_from >= want_to:
        want_from, want_to = want_to, want_from

    cov_min, cov_max, cached = await candle_coverage(db, figi, interval_value)
    step = _INTERVAL_STEP[interval_name]
    ranges = missing_ranges(cov_min, cov_max, want_from, want_to, step)

    downloaded = 0
    for range_from_, range_to_ in ranges:
        rows = await to_thread(fetch_candles, figi, interval, range_from_, range_to_)
        if rows:
            downloaded += await upsert_candles(db, rows)

    new_cov_min, new_cov_max, _ = await candle_coverage(db, figi, interval_value)
    return {
        "figi": figi,
        "interval": interval_name,
        "cached_bars": cached,
        "downloaded": downloaded,
        "requests": len(ranges),
        "coverage_from": (new_cov_min or cov_min).isoformat() if (new_cov_min or cov_min) else None,
        "coverage_to": (new_cov_max or cov_max).isoformat() if (new_cov_max or cov_max) else None,
    }
