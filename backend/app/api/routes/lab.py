from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_db
from app.services.experiments import (
    EXIT_POLICY_SPECS,
    ExperimentError,
    batch,
    create_and_run_experiment,
    get_experiment,
    list_experiments,
    sweep,
)

router = APIRouter(prefix="/api/v1", tags=["lab"])


class ExperimentRequest(BaseModel):
    figi: str
    interval_name: str = "hour"
    days: int = 120
    strategy_id: str | None = None
    params: dict = Field(default_factory=dict)
    strategy_run_id: UUID | None = None
    signal_policy: dict = Field(default_factory=lambda: {"id": "ignore_same_side", "params": {}})
    exit_policy: dict = Field(default_factory=lambda: {"id": "fixed_sl_tp", "params": {}})
    session: dict | None = None
    qty: int = 1
    allow_short: bool = True
    cost_model: dict = Field(default_factory=dict)
    purpose: str = "DESIGN"


class SweepRequest(BaseModel):
    figi: str
    interval_name: str = "hour"
    days: int = 120
    strategy_id: str | None = None
    params: dict = Field(default_factory=dict)
    exit_policy: dict = Field(default_factory=lambda: {"id": "fixed_sl_tp"})
    grid: dict = Field(default_factory=dict)
    max_trials: int = 12
    qty: int = 1


class BatchRequest(BaseModel):
    interval_name: str = "hour"
    days: int = 60
    top_n: int = 10
    tickers: list[str] | None = None
    strategy_id: str | None = None
    params: dict = Field(default_factory=dict)
    exit_policy: dict = Field(default_factory=lambda: {"id": "fixed_sl_tp", "params": {}})
    qty: int = 1


def _err(e: ExperimentError) -> HTTPException:
    return HTTPException(400, str(e))


@router.post("/experiments")
async def create_experiment(req: ExperimentRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await create_and_run_experiment(db, req.model_dump())
    except ExperimentError as e:
        raise _err(e)


@router.get("/experiments")
async def experiments_list(
    figi: str | None = None, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> dict:
    items = await list_experiments(db, figi, limit)
    return {"count": len(items), "experiments": items}


@router.get("/experiments/{exp_id}")
async def experiment_one(exp_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await get_experiment(db, exp_id)
    except ExperimentError as e:
        raise HTTPException(404, str(e))


@router.post("/lab/sweep")
async def lab_sweep(req: SweepRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await sweep(db, req.model_dump())
    except ExperimentError as e:
        raise _err(e)


@router.post("/lab/batch")
async def lab_batch(req: BatchRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await batch(db, req.model_dump())
    except ExperimentError as e:
        raise _err(e)


@router.get("/policies/catalog")
async def policies_catalog() -> dict:
    exits = [
        {
            "id": pid,
            "label": spec["label"],
            "params_schema": spec["schema"],
        }
        for pid, spec in EXIT_POLICY_SPECS.items()
    ]
    signals = [
        {
            "id": "ignore_same_side",
            "label": "Ignore same side",
            "params_schema": {
                "min_hold_bars": {"type": "int", "default": 0, "min": 0, "max": 200}
            },
        }
    ]
    sessions = [
        {
            "id": "moex_intraday_v1",
            "label": "MOEX intraday",
            "params_schema": {
                "entry_cutoff_bars": {"type": "int", "default": 0, "min": 0, "max": 60},
                "overnight": {"type": "bool", "default": True},
            },
        }
    ]
    return {"exit_policies": exits, "signal_policies": signals, "session_policies": sessions}


from uuid import UUID as _UUID
from datetime import datetime as _dt

from app.models.test_runs import TestRun
from app.models.configurations import Configuration
from app.services.test_queue import (
    enqueue,
    get_setting,
    max_concurrent,
    set_setting,
    set_status,
)


class QueueItemBody(BaseModel):
    config_id: _UUID
    tickers: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    period_days: int = 30


@router.post("/lab/queue")
async def lab_queue_create(body: QueueItemBody, db: AsyncSession = Depends(get_db)) -> dict:
    cfg = await db.get(Configuration, body.config_id)
    if cfg is None:
        raise HTTPException(404, "configuration not found")
    df = dt_ = None
    if body.date_from:
        df = _dt.fromisoformat(body.date_from.replace("Z", "+00:00"))
        if df.tzinfo is None:
            from datetime import timezone

            df = df.replace(tzinfo=timezone.utc)
    if body.date_to:
        dt_ = _dt.fromisoformat(body.date_to.replace("Z", "+00:00"))
        if dt_.tzinfo is None:
            from datetime import timezone

            dt_ = dt_.replace(tzinfo=timezone.utc)
    # ансамблевая конфигурация? (фильтр ensemble с параметрами из модалки)
    ens = next((f for f in (cfg.filters or []) if isinstance(f, dict) and f.get("id") == "ensemble"), None)
    if ens and ens.get("params"):
        p = ens["params"]
        from app.models.ensemble_runs import EnsembleRun as _ER
        # тикеры → figi (в filters храним тикеры, а движку нужны figi)
        tickers = body.tickers or (p.get("tickers") or [])
        figis = []
        if tickers:
            from app.models.instrument import Instrument as _Inst
            inst_rows = (await db.execute(
                select(_Inst).where(_Inst.ticker.in_([t.upper() for t in tickers]))
            )).scalars().all()
            figis = [r.figi for r in inst_rows]
            if not figis:
                figis = [t for t in tickers if str(t).startswith("BBG")]
        params = {
            "figis": figis,
            "days": body.period_days or 30,
            "bias_mode": p.get("bias_mode", "info"),
            "entry_tf": p.get("entry_tf", "5min"),
            "entry_session": p.get("entry_session", "main"),
            "carry_overnight": True,
            "quorum": cfg.quorum,
            "same_side_reentry_cooldown_bars": p.get("cooldown_bars", 15),
            "capital": 100000,
            "lot": 10,
            "use_all_setups": True,
            "drop_useless": True,
            "setups": [{"strategy_id": m["strategy_id"], "tf": "5min", "params": m.get("params", {})} for m in cfg.members],
            "exit_policy": cfg.exit_policy,
            "entry": {"tf": p.get("entry_tf", "5min"), "lookback": p.get("entry_lookback", 1)},
            "bias": {"tf": "hour", "period": p.get("bias_period", 50)},
            "from_ts": ((df.isoformat() if df else None) or p.get("date_from")),
            "to_ts": ((dt_.isoformat() if dt_ else None) or p.get("date_to")),
        }
        run = _ER(status="QUEUED", params=params,
                  progress={"done": 0, "total": len(params["figis"]), "current": "", "by_stock": {}})
        db.add(run)
        await db.commit()
        from app.services.ensemble_queue import queue_dispatcher
        queue_dispatcher.start()
        return {"run_id": str(run.id), "status": run.status, "queued": True, "mode": "ensemble"}
    run = await enqueue(body.config_id, body.tickers, df, dt_, body.period_days)
    return {"run_id": str(run.id), "status": run.status, "queued": True}


@router.get("/lab/queue")
async def lab_queue_list(db: AsyncSession = Depends(get_db)) -> dict:
    runs = (
        await db.execute(
            select(TestRun).order_by(TestRun.created_at)
        )
    ).scalars().all()
    items = []
    for r in runs:
        cfg = await db.get(Configuration, r.config_id)
        items.append(
            {
                "run_id": str(r.id),
                "config_id": str(r.config_id),
                "config_name": cfg.name if cfg else "?",
                "members": (cfg.members if cfg else []),
                "quorum": cfg.quorum if cfg else 0,
                "exit_policy": cfg.exit_policy if cfg else {},
                "interval_name": cfg.interval_name if cfg else "",
                "min_hold_bars": cfg.min_hold_bars if cfg else 0,
                "allow_short": cfg.allow_short if cfg else False,
                "status": r.status,
                "tickers": r.tickers,
                "date_from": r.date_from.isoformat() if r.date_from else None,
                "date_to": r.date_to.isoformat() if r.date_to else None,
                "period_days": r.period_days,
                "progress": r.progress,
                "error": r.error,
                "lab_result": cfg.lab_result if cfg else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return {"max_concurrent": await max_concurrent(), "count": len(items), "runs": items}


@router.post("/lab/queue/{run_id}/pause")
async def lab_queue_pause(run_id: _UUID) -> dict:
    from app.services.test_queue import queue_dispatcher
    async with SessionLocal() as _db:
        run = await _db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status == "RUNNING":
            queue_dispatcher.cancel_run(run_id)
            await set_status(run_id, "QUEUED")
        elif run.status == "QUEUED":
            await set_status(run_id, "PAUSED")
        return {"run_id": str(run_id), "status": (await _db.get(TestRun, run_id)).status}


@router.post("/lab/queue/{run_id}/resume")
async def lab_queue_resume(run_id: _UUID) -> dict:
    async with SessionLocal() as _db:
        run = await _db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status == "PAUSED":
            await set_status(run_id, "QUEUED")
        return {"run_id": str(run_id), "status": (await _db.get(TestRun, run_id)).status}


@router.post("/lab/queue/{run_id}/stop")
async def lab_queue_stop(run_id: _UUID) -> dict:
    from app.services.test_queue import queue_dispatcher
    async with SessionLocal() as _db:
        run = await _db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        queue_dispatcher.cancel_run(run_id)
        await _db.delete(run)
        cfg = await _db.get(Configuration, run.config_id)
        if cfg is not None and cfg.status not in ("DRAFT",):
            cfg.status = "DRAFT"
        await _db.commit()
        return {"run_id": str(run_id), "status": "CANCELLED", "returned_to_left": True}


@router.delete("/lab/runs/{run_id}")
async def lab_run_delete(run_id: _UUID) -> dict:
    async with SessionLocal() as _db:
        run = await _db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await _db.delete(run)
        await _db.commit()
        return {"deleted": True}


@router.post("/lab/runs/{run_id}/recycle")
async def lab_run_recycle(run_id: _UUID) -> dict:
    async with SessionLocal() as _db:
        run = await _db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await _db.delete(run)
        cfg = await _db.get(Configuration, run.config_id)
        if cfg is not None:
            cfg.status = "DRAFT"
        await _db.commit()
        return {"recycled": True}


class SettingsBody(BaseModel):
    max_concurrent_tests: int | None = None
    commission_rate: float | None = Field(None, ge=0.0, le=0.05)
    slippage_bps: float | None = Field(None, ge=0.0, le=200.0)


@router.get("/lab/settings")
async def lab_settings_get() -> dict:
    return {
        "max_concurrent_tests": await max_concurrent(),
        "commission_rate": float(await get_setting("commission_rate", "0.0005")),
        "slippage_bps": float(await get_setting("slippage_bps", "2.0")),
    }


@router.put("/lab/settings")
async def lab_settings_put(body: SettingsBody) -> dict:
    if body.max_concurrent_tests is not None:
        await set_setting("max_concurrent_tests", str(max(1, body.max_concurrent_tests)))
    if body.commission_rate is not None:
        await set_setting("commission_rate", str(body.commission_rate))
    if body.slippage_bps is not None:
        await set_setting("slippage_bps", str(body.slippage_bps))
    return {
        "max_concurrent_tests": await max_concurrent(),
        "commission_rate": float(await get_setting("commission_rate", "0.0005")),
        "slippage_bps": float(await get_setting("slippage_bps", "2.0")),
    }


# ==================== Ансамблевые прогоны (Lab → Ансамбль) ====================

class EnsembleRunBody(BaseModel):
    figis: list[str] = Field(default_factory=list)
    days: int = Field(30, ge=1, le=120)
    bias_mode: str = Field("info", pattern="^(veto|info|strict_ct)$")
    entry_tf: str = Field("5min", pattern="^(1min|5min)$")
    entry_session: str = Field("main", pattern="^(all|main)$")
    carry_overnight: bool = True
    force_flat_at_session_end: bool = False
    quorum: int = Field(2, ge=1, le=5)
    same_side_reentry_cooldown_bars: int = Field(15, ge=0, le=1440)
    capital: float = Field(100_000, ge=1000, le=100_000_000)
    lot: int = Field(10, ge=1, le=1000)
    use_all_setups: bool = True
    drop_useless: bool = True
    setups: list[dict] = Field(default_factory=list)  # [{strategy_id, tf, params}]
    entry: dict = Field(default_factory=lambda: {"tf": "1min", "lookback": 1})
    exit_policy: dict = Field(default_factory=lambda: {
        "id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2}})
    from_ts: str | None = None
    to_ts: str | None = None
    commission_rate: float = Field(0.0005, ge=0.0, le=0.05)
    slippage_bps: float = Field(2.0, ge=0.0, le=200.0)


@router.post("/lab/ensemble", status_code=201)
async def ensemble_run_create(body: EnsembleRunBody) -> dict:
    from app.models.ensemble_runs import EnsembleRun
    from datetime import datetime, timezone

    if not body.figis:
        raise HTTPException(400, "figis пуст")
    params = body.model_dump()
    # настройки Lab по умолчанию (комиссия/слип), если не заданы явно
    if body.commission_rate == 0.0005 and body.slippage_bps == 2.0:
        params["commission_rate"] = float(await get_setting("commission_rate", "0.0005"))
        params["slippage_bps"] = float(await get_setting("slippage_bps", "2.0"))
    run = EnsembleRun(
        status="QUEUED",
        params=params,
        progress={"done": 0, "total": len(body.figis), "current": "", "by_stock": {}},
    )
    async with SessionLocal() as db:
        db.add(run)
        await db.commit()
        run_id = run.id
    from app.services.ensemble_queue import queue_dispatcher
    queue_dispatcher.start()
    return {"run_id": str(run_id), "status": "QUEUED", "queued": True}


@router.get("/lab/ensemble")
async def ensemble_run_list(limit: int = 20) -> dict:
    from app.models.ensemble_runs import EnsembleRun
    from sqlalchemy import desc

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(EnsembleRun).order_by(desc(EnsembleRun.created_at)).limit(limit)
        )).scalars().all()
        return {
            "count": len(rows),
            "runs": [
                {
                    "run_id": str(r.id),
                    "status": r.status,
                    "params": r.params,
                    "progress": r.progress,
                    "result": r.result,
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }


@router.post("/lab/ensemble/{run_id}/cancel")
async def ensemble_run_cancel(run_id: UUID) -> dict:
    from app.models.ensemble_runs import EnsembleRun
    from app.services.ensemble_queue import set_status

    async with SessionLocal() as db:
        run = await db.get(EnsembleRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status in ("QUEUED", "RUNNING"):
            await set_status(run_id, "CANCELLED")
            return {"cancelled": True}
        return {"cancelled": False, "status": run.status}


@router.delete("/lab/ensemble/{run_id}")
async def ensemble_run_delete(run_id: UUID) -> dict:
    from app.models.ensemble_runs import EnsembleRun

    async with SessionLocal() as db:
        run = await db.get(EnsembleRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await db.delete(run)
        await db.commit()
        return {"deleted": True}
