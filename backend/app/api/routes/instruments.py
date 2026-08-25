from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.instrument import Instrument
from app.schemas.market import InstrumentOut
from app.services.tinvest import fetch_shares, to_thread, upsert_instruments

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


@router.post("/sync")
async def sync_instruments(db: AsyncSession = Depends(get_db)) -> dict:
    shares = await to_thread(fetch_shares)
    count = await upsert_instruments(db, shares)
    return {"synced": count}


@router.get("")
async def list_instruments(
    search: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(Instrument).order_by(Instrument.ticker)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Instrument.ticker.ilike(pattern), Instrument.name.ilike(pattern)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.offset(offset).limit(limit))
    return {"total": total, "items": [InstrumentOut.model_validate(r) for r in result.scalars()]}


@router.get("/{figi}")
async def get_instrument(figi: str, db: AsyncSession = Depends(get_db)) -> InstrumentOut:
    instrument = await db.scalar(select(Instrument).where(Instrument.figi == figi))
    if not instrument:
        raise HTTPException(404, "Instrument not found")
    return InstrumentOut.model_validate(instrument)
