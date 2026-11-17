import asyncio
from collections import Counter
from datetime import timedelta, timezone

from app.services.macro_candle_source import read_closed_macro_candles


async def sync_macro_series(db, instrument, start, end, *, fetch=read_closed_macro_candles, persist=None):
    if persist is None:
        from app.services.tinvest import upsert_candles_detailed

        persist = upsert_candles_detailed
    bars = await asyncio.to_thread(fetch, instrument, start, end)
    rows = [
        dict(bar, figi=instrument.storage_id, interval=1)
        for bar in bars
        if start <= bar["ts"] and bar["ts"] + timedelta(minutes=1) <= end
    ]
    return await persist(db, rows, window=(start, end))


async def _latest_macro_minute(db, instrument, as_of):
    from sqlalchemy import func, select

    from app.models.candle import Candle

    return await db.scalar(
        select(func.max(Candle.ts)).where(
            Candle.figi == instrument.storage_id,
            Candle.interval == 1,
            Candle.ts <= as_of - timedelta(minutes=1),
        )
    )


async def sync_macro_tail(db, instrument, *, history_start, as_of, fetch=read_closed_macro_candles, persist=None):
    from app.services.candle_ingestion_contract import UpsertResult

    for instant in (history_start, as_of):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Timezone-aware macro tail bounds required")
    if history_start > as_of:
        raise ValueError("Macro tail starts after as_of")

    minute = timedelta(minutes=1)
    latest = await _latest_macro_minute(db, instrument, as_of)
    start = max(history_start, latest) if latest is not None else history_start
    start = start.astimezone(timezone.utc)
    floor = start.replace(second=0, microsecond=0)
    start = floor + minute if start != floor else floor
    totals = Counter()
    while start + minute <= as_of:
        end = min(start + timedelta(days=7), as_of)
        chunk = await sync_macro_series(db, instrument, start, end, fetch=fetch, persist=persist)
        totals.update(vars(chunk))
        start = end
    return UpsertResult(**totals)


async def sync_macro_cycle(session_factory, instruments, *, history_start, as_of):
    unique = {instrument.storage_id: instrument for instrument in instruments}
    slots = asyncio.Semaphore(3)

    async def sync_one(instrument):
        try:
            async with slots, session_factory() as db:
                written = await sync_macro_tail(db, instrument, history_start=history_start, as_of=as_of)
                latest = await _latest_macro_minute(db, instrument, as_of)
        except Exception as error:
            return {"status": "error", "error_type": type(error).__name__}
        return {
            "status": "partial" if written.rejected else "received" if written.accepted else "no_rows",
            "last_stored_close_at": (latest.astimezone(timezone.utc) + timedelta(minutes=1)).isoformat() if latest is not None else None,
            "inserted": written.inserted,
            "unchanged": written.unchanged,
            "corrected": written.corrected,
            "rejected": written.rejected,
        }

    reports = await asyncio.gather(*(sync_one(instrument) for instrument in unique.values()))
    return dict(zip(unique, reports, strict=True))
