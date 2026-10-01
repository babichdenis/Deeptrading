import math
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

# Шаг агрегации каждого интервала в секундах (для ресэмплинга из 1m):
_INTERVAL_STEP_SEC = {
    "1min": 60,
    "5min": 300,
    "10min": 600,
    "15min": 900,
    "hour": 3600,
    "2h": 7200,
    "4h": 14400,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
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


async def resample_from_1m(
    db: AsyncSession,
    figi: str,
    interval_name: str,
    want_from: datetime,
    want_to: datetime,
) -> int:
    """Заполнить старшие ТФ агрегацией минутных свечей из БД.

    Канон (REF-001b, 30.09): метка бара = НАЧАЛО бакета (эпоха, floor) —
    совпадает с marketdata.Resampler и SQL date_bin. Раньше здесь было
    ceil/закрытие («как T-Invest»), из-за чего прогрев бота шёл по другой
    сетке, чем live/replay-бары (смешение у стыка). open=первая 1m, high=max,
    low=min, close=последняя 1m, volume=sum. Возвращает число записанных баров.
    """
    if interval_name == "1min":
        return 0
    step_sec = _INTERVAL_STEP_SEC.get(interval_name)
    if step_sec is None:
        return 0
    target_interval = INTERVAL_NAMES[interval_name]
    target_interval_value = int(target_interval.value)

    rows = (
        await db.execute(
            select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
            .where(
                Candle.figi == figi,
                Candle.interval == 1,
                Candle.ts >= want_from,
                Candle.ts <= want_to,
            )
            .order_by(Candle.ts)
        )
    ).all()

    if not rows:
        return 0

    buckets: dict[int, dict] = {}
    for ts, open_, high, low, close, volume in rows:
        unix = int(ts.replace(tzinfo=timezone.utc).timestamp())
        bucket_ts = unix - (unix % step_sec)  # метка = НАЧАЛО бакета (канон: Resampler/date_bin)
        b = buckets.setdefault(bucket_ts, {
            "ts": bucket_ts, "open": open_, "high": high, "low": low, "close": close, "volume": 0,
        })
        b["high"] = max(b["high"], high)
        b["low"] = min(b["low"], low)
        b["close"] = close  # по возрастанию ts — последняя перезапишет
        b["volume"] = b["volume"] + volume

    from decimal import Decimal
    candles = []
    for bucket_ts in sorted(buckets):
        b = buckets[bucket_ts]
        candles.append({
            "figi": figi,
            "interval": target_interval_value,
            "ts": datetime.fromtimestamp(b["ts"], tz=timezone.utc),
            "open": Decimal(b["open"]),
            "high": Decimal(b["high"]),
            "low": Decimal(b["low"]),
            "close": Decimal(b["close"]),
            "volume": b["volume"],
        })

    if not candles:
        return 0
    return await upsert_candles(db, candles)


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

    downloaded = 0
    # Сначала пробуем добить из минутных свечей (быстро, без API)
    if interval_name != "1min":
        tail_from = cov_max + step if cov_max else want_from
        if tail_from < want_to:
            resampled = await resample_from_1m(db, figi, interval_name, tail_from, want_to)
            if resampled:
                downloaded += resampled
                cov_min, cov_max, _ = await candle_coverage(db, figi, interval_value)

    ranges = missing_ranges(cov_min, cov_max, want_from, want_to, step)

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