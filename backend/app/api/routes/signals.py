from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.signals import compute_signals, get_run, list_runs

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])


class ComputeRequest(BaseModel):
    figi: str
    interval_name: str = "day"
    strategy_id: str
    params: dict = Field(default_factory=dict)
    days: int = 120
    force: bool = False
    from_ts: str | None = None
    to_ts: str | None = None


@router.post("/compute")
async def compute(req: ComputeRequest, db: AsyncSession = Depends(get_db)) -> dict:
    date_from = date_to = None
    try:
        if req.from_ts:
            date_from = datetime.fromisoformat(req.from_ts.replace("Z", "+00:00"))
        if req.to_ts:
            date_to = datetime.fromisoformat(req.to_ts.replace("Z", "+00:00"))
        return await compute_signals(
            db, req.figi, req.interval_name, req.strategy_id, req.params,
            days=req.days, force=req.force, date_from=date_from, date_to=date_to,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/runs")
async def runs(figi: str | None = None, limit: int = 50, db: AsyncSession = Depends(get_db)) -> dict:
    items = await list_runs(db, figi, min(limit, 200))
    return {"count": len(items), "runs": items}


@router.get("/{run_id}")
async def one(run_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    run = await get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run
