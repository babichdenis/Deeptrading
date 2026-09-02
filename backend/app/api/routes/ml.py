from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ml import MlModel, MlPrediction
from app.services.ml import predict_for_run, train_ml_model

router = APIRouter(prefix="/api/v1/ml", tags=["ml"])


class TrainRequest(BaseModel):
    figi: str
    interval_name: str = "hour"
    strategy_id: str | None = None
    params: dict = Field(default_factory=dict)
    days: int = 120
    horizon_bars: int = 20
    r_multiple: float = 1.0


class PredictRequest(BaseModel):
    model_id: UUID
    run_id: UUID | None = None
    threshold: float = 0.5


class MetaTrainRequest(BaseModel):
    """Обучение ML meta-filter (meta-labeling) по ансамблевым кандидатам.

    Данные: либо csv_dir (прямое чтение CSV-архивов T-Invest), либо figis + БД.
    Периоды задаются в UTC ISO.
    """
    csv_dir: str | None = None
    figis: list[str] = Field(default_factory=list)
    train_from: str
    train_to: str
    val_from: str
    val_to: str
    oos_from: str
    oos_to: str
    max_hold_bars: int = 60
    purge_bars: int = 60
    threshold_candidates: list[float] = Field(default_factory=lambda: [0.45, 0.50, 0.55, 0.60])


@router.post("/meta/train")
async def ml_meta_train(req: MetaTrainRequest) -> dict:
    """Строит датасет кандидатов (raw/quorum/entry) и обучает мета-фильтр.

    Возвращает отчёт: метрики baseline vs ML на OOS, калибровка, counterfactual,
    leakage checks, feature importance.
    """
    import asyncio
    from datetime import datetime, timezone

    from app.engine.models import Candle as EngineCandle
    from app.services.ml_meta import build_dataset, train_meta_filter

    def parse(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    # 1) свечи: из CSV или из БД
    candles_by_figi: dict[str, list[EngineCandle]] = {}
    if req.csv_dir:
        import glob
        import os

        from app.services.ml_meta import UID_FIGI_ALIASES

        files = sorted(glob.glob(os.path.join(req.csv_dir, "*.csv")))
        for path in files:
            uid = os.path.basename(path).split("_")[0]
            figi = UID_FIGI_ALIASES.get(uid)
            if not figi or (req.figis and figi not in req.figis):
                continue
            if figi not in candles_by_figi:
                candles_by_figi[figi] = []
            with open(path) as f:
                for line in f:
                    parts = line.strip().rstrip(";").split(";")
                    if len(parts) < 7:
                        continue
                    _, ts, o, c, h, l, v = parts[:7]
                    try:
                        candles_by_figi[figi].append(EngineCandle(
                            ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
                            open=float(o), high=float(h), low=float(l),
                            close=float(c), volume=float(v)))
                    except (ValueError, IndexError):
                        continue
        for figi in candles_by_figi:
            candles_by_figi[figi].sort(key=lambda c: c.ts)
    else:
        raise HTTPException(400, "пока поддерживается только csv_dir")

    if not candles_by_figi:
        raise HTTPException(400, "нет свечей в csv_dir")

    # 2) датасет
    rows, meta = await asyncio.to_thread(
        build_dataset, candles_by_figi, None, req.max_hold_bars)

    # 3) обучение (walk-forward, purge/embargo)
    result = await asyncio.to_thread(
        train_meta_filter,
        rows,
        parse(req.train_from), parse(req.train_to),
        parse(req.val_from), parse(req.val_to),
        parse(req.oos_from), parse(req.oos_to),
        purge_bars=req.purge_bars,
        threshold_candidates=req.threshold_candidates,
    )
    result["dataset"] = meta
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


@router.post("/meta/dataset")
async def ml_meta_dataset(req: MetaTrainRequest) -> dict:
    """Только построение датасета (без обучения) — для инспекции строк."""
    import asyncio
    from datetime import datetime, timezone

    from app.engine.models import Candle as EngineCandle
    from app.services.ml_meta import build_dataset, UID_FIGI_ALIASES

    if not req.csv_dir:
        raise HTTPException(400, "нужен csv_dir")
    import glob
    import os

    candles_by_figi: dict[str, list[EngineCandle]] = {}
    for path in sorted(glob.glob(os.path.join(req.csv_dir, "*.csv"))):
        uid = os.path.basename(path).split("_")[0]
        figi = UID_FIGI_ALIASES.get(uid)
        if not figi or (req.figis and figi not in req.figis):
            continue
        if figi not in candles_by_figi:
            candles_by_figi[figi] = []
        with open(path) as f:
            for line in f:
                parts = line.strip().rstrip(";").split(";")
                if len(parts) < 7:
                    continue
                _, ts, o, c, h, l, v = parts[:7]
                try:
                    candles_by_figi[figi].append(EngineCandle(
                        ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
                        open=float(o), high=float(h), low=float(l),
                        close=float(c), volume=float(v)))
                except (ValueError, IndexError):
                    continue
    rows, meta = await asyncio.to_thread(build_dataset, candles_by_figi, None, req.max_hold_bars)
    return {"dataset": meta, "sample": rows[:3]}


@router.post("/train")
async def ml_train(req: TrainRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await train_ml_model(
            db,
            req.figi,
            req.interval_name,
            req.strategy_id,
            req.params,
            days=req.days,
            horizon_bars=req.horizon_bars,
            r_multiple=req.r_multiple,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/models")
async def ml_models(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(select(MlModel).order_by(desc(MlModel.created_at)).limit(50))
    ).scalars().all()
    return {
        "count": len(rows),
        "models": [
            {
                "model_id": str(m.id),
                "name": m.name,
                "figi": m.figi,
                "interval_name": m.interval_name,
                "strategy_id": m.strategy_id,
                "target_spec": m.target_spec,
                "metrics": m.metrics,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ],
    }


@router.post("/predict")
async def ml_predict(req: PredictRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        if req.run_id is not None:
            return await predict_for_run(db, req.model_id, req.run_id, req.threshold)
        model = await db.scalar(select(MlModel).where(MlModel.id == req.model_id))
        if model is None:
            raise HTTPException(404, "model not found")
        return await predict_for_run(db, req.model_id, model.strategy_run_id, req.threshold)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/models/{model_id}/predictions")
async def ml_predictions(model_id: UUID, limit: int = 100, db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(
            select(MlPrediction)
            .where(MlPrediction.model_id == model_id)
            .order_by(desc(MlPrediction.probability))
            .limit(min(limit, 500))
        )
    ).scalars().all()
    return {
        "count": len(rows),
        "predictions": [
            {
                "signal_id": p.signal_id,
                "ts": p.ts.isoformat(),
                "side": p.side,
                "probability": float(p.probability),
                "decision": p.decision,
            }
            for p in rows
        ],
    }
