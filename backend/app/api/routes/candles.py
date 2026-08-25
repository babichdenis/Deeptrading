from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candle import Candle
from app.models.instrument import Instrument
from app.services.candle_cache import DEFAULT_DAYS, candle_coverage, ensure_candles
from app.services.tinvest import INTERVAL_NAMES

router = APIRouter(prefix="/api/candles", tags=["candles"])


async def _check_instrument(db: AsyncSession, figi: str) -> None:
    instrument = await db.scalar(select(Instrument).where(Instrument.figi == figi))
    if not instrument:
        raise HTTPException(404, "Instrument not found, call /api/instruments/sync first")


@router.post("/{figi}/sync")
async def sync_candles(
    figi: str,
    interval_name: str = Query("day", description=f"Интервал: {', '.join(INTERVAL_NAMES)}"),
    days: int = Query(DEFAULT_DAYS, ge=1, le=3650),
    from_ts: str | None = Query(None, description="ISO datetime начала диапазона"),
    to_ts: str | None = Query(None, description="ISO datetime конца диапазона"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if interval_name not in INTERVAL_NAMES:
        raise HTTPException(400, f"Unknown interval. Available: {', '.join(INTERVAL_NAMES)}")
    await _check_instrument(db, figi)

    range_from = range_to = None
    try:
        if from_ts:
            range_from = datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
        if to_ts:
            range_to = datetime.fromisoformat(to_ts.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"bad from/to: {e}")

    report = await ensure_candles(
        db, figi, interval_name, days, range_from=range_from, range_to=range_to
    )
    return report


@router.get("/{figi}/coverage")
async def get_coverage(figi: str, db: AsyncSession = Depends(get_db)) -> dict:
    await _check_instrument(db, figi)
    rows = await db.execute(
        select(
            Candle.interval,
            func.min(Candle.ts),
            func.max(Candle.ts),
            func.count(),
        )
        .where(Candle.figi == figi)
        .group_by(Candle.interval)
        .order_by(Candle.interval)
    )
    name_by_value = {int(getattr(v, "value", v)): k for k, v in INTERVAL_NAMES.items()}
    intervals = {}
    for ivalue, cov_min, cov_max, count in rows.all():
        key = name_by_value.get(ivalue, str(ivalue))
        intervals[key] = {
            "from": cov_min.isoformat(),
            "to": cov_max.isoformat(),
            "bars": count,
        }
    return {"figi": figi, "intervals": intervals}


@router.get("/{figi}")
async def get_candles(
    figi: str,
    interval_name: str = Query("day"),
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    interval = INTERVAL_NAMES.get(interval_name)
    if interval is None:
        raise HTTPException(400, f"Unknown interval. Available: {', '.join(INTERVAL_NAMES)}")

    interval_value = int(getattr(interval, "value", interval))
    rows = await db.execute(
        select(Candle)
        .where(Candle.figi == figi, Candle.interval == interval_value)
        .order_by(desc(Candle.ts))
        .limit(limit)
    )
    candles = list(reversed(rows.scalars().all()))
    return {
        "figi": figi,
        "interval": interval_name,
        "count": len(candles),
        "candles": [
            {
                "ts": c.ts.isoformat(),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": c.volume,
            }
            for c in candles
        ],
    }
