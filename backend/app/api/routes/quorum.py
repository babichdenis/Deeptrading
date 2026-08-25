from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.decisions import POLICY_IGNORE_SAME_SIDE, compute_decisions
from app.services.quorum import compute_quorum
from app.services.signals import get_run

router = APIRouter(prefix="/api/v1", tags=["quorum"])


class QuorumRequest(BaseModel):
    member_run_ids: list[UUID] = Field(min_length=2)
    k: int = 2


class DecisionsRequest(BaseModel):
    policy_id: str = POLICY_IGNORE_SAME_SIDE
    min_hold_bars: int = 0


@router.post("/quorum/compute")
async def quorum_compute(req: QuorumRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        result = await compute_quorum(db, req.member_run_ids, req.k)
        result["run"] = await get_run(db, UUID(result["run_id"]))
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/signals/{run_id}/decisions")
async def decisions_compute(
    run_id: UUID, req: DecisionsRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    if not (0 <= req.min_hold_bars <= 1000):
        raise HTTPException(400, "min_hold_bars out of range")
    try:
        return await compute_decisions(
            db, run_id, policy_id=req.policy_id, min_hold_bars=req.min_hold_bars
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
