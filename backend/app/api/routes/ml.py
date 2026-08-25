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
