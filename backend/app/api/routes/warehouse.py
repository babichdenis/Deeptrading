from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.experiments import ExperimentError
from app.models.configurations import Configuration as ConfigurationModel
from app.services.warehouse import (
    ConfigurationError,
    create_configuration,
    fork_configuration,
    get_configuration,
    list_configurations,
    preview_configuration,
    send_to_lab,
    update_configuration,
)

router = APIRouter(prefix="/api/v1/warehouse", tags=["warehouse"])


class MemberIn(BaseModel):
    strategy_id: str
    params: dict = Field(default_factory=dict)
    role: str = "setup"  # bias | setup | entry


class ConfigurationBody(BaseModel):
    name: str = "Без названия"
    interval_name: str = "hour"
    members: list[MemberIn] = Field(min_length=1)
    quorum: int = 1
    exit_policy: dict = Field(default_factory=lambda: {"id": "fixed_sl_tp", "params": {}})
    min_hold_bars: int = 0
    allow_short: bool = False
    session_policy: dict = Field(default_factory=lambda: {"entry_cutoff_bars": 0, "overnight": True})
    filters: list = Field(default_factory=list)
    figi: str | None = None


class PreviewBody(BaseModel):
    figi: str | None = None
    days: int = 120


class SendToLabBody(BaseModel):
    period_days: int = 30
    top_n: int = 10
    tickers: list[str] | None = None
    date_from: str | None = None
    date_to: str | None = None


@router.post("/configurations")
async def wh_create(body: ConfigurationBody, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await create_configuration(db, body.model_dump())
    except (ConfigurationError, ExperimentError) as e:
        code = 400
        msg = str(e)
        if "out of bounds" in msg or "unknown" in msg:
            raise HTTPException(400, msg)
        raise HTTPException(500, msg)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/configurations")
async def wh_list(limit: int = 50, db: AsyncSession = Depends(get_db)) -> dict:
    items = await list_configurations(db, limit)
    return {"count": len(items), "configurations": items}


@router.get("/configurations/{config_id}")
async def wh_get(config_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    return await _safe(lambda: get_configuration(db, config_id))


@router.patch("/configurations/{config_id}")
async def wh_update(config_id: UUID, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    return await _safe(lambda: update_configuration(db, config_id, body))


@router.post("/configurations/{config_id}/preview")
async def wh_preview(config_id: UUID, body: PreviewBody, db: AsyncSession = Depends(get_db)) -> dict:
    return await _safe(lambda: preview_configuration(db, config_id, body.figi, body.days))


@router.delete("/configurations/{config_id}")
async def wh_delete(config_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(ConfigurationModel, config_id)
    if row is None:
        raise HTTPException(404, "configuration not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


@router.post("/configurations/{config_id}/clone")
async def wh_clone(config_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    return await _safe(lambda: fork_configuration(db, config_id))


@router.post("/configurations/{config_id}/send-to-lab")
async def wh_send_to_lab(config_id: UUID, body: SendToLabBody, db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import datetime
    df = dt_ = None
    if body.date_from:
        df = datetime.fromisoformat(body.date_from.replace("Z", "+00:00"))
    if body.date_to:
        dt_ = datetime.fromisoformat(body.date_to.replace("Z", "+00:00"))
    return await _safe(lambda: send_to_lab(db, config_id, body.period_days, body.top_n,
                                           body.tickers, date_from=df, date_to=dt_))


background_tasks: set = set()


@router.post("/configurations/{config_id}/send-to-lab-async")
async def wh_send_to_lab_async(config_id: UUID, body: SendToLabBody) -> dict:
    import asyncio as _aio

    async def job():
        from app.database import SessionLocal
        from datetime import datetime
        df = dt_ = None
        try:
            if body.date_from:
                df = datetime.fromisoformat(body.date_from.replace("Z", "+00:00"))
            if body.date_to:
                dt_ = datetime.fromisoformat(body.date_to.replace("Z", "+00:00"))
            async with SessionLocal() as db2:
                await send_to_lab(db2, config_id, body.period_days, body.top_n,
                                  body.tickers, date_from=df, date_to=dt_)
        except Exception:
            from app.database import SessionLocal as SL2
            async with SL2() as db3:
                row = await db3.get(ConfigurationModel, config_id)
                if row is not None:
                    row.status = "FAILED"
                    row.lab_result = {**(row.lab_result or {}), "error": "run failed"}
                    await db3.commit()

    task = _aio.create_task(job())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {"started": True, "status": "IN_LAB"}


async def _safe(fn):
    try:
        return await fn()
    except (ConfigurationError, ExperimentError) as e:
        msg = str(e)
        code = 404 if ("not found" in msg or "не найдена" in msg) else 400
        raise HTTPException(code, msg)
