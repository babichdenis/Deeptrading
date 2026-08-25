import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MlModel(Base):
    __tablename__ = "ml_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    name: Mapped[str] = mapped_column(String(128))
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_runs.id", ondelete="SET NULL"), index=True
    )
    figi: Mapped[str] = mapped_column(String(32))
    interval_name: Mapped[str] = mapped_column(String(16))
    features_version: Mapped[str] = mapped_column(String(16), default="v1")
    target_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_names: Mapped[list] = mapped_column(JSON, default=list)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="TRAINED")


class MlPrediction(Base):
    __tablename__ = "ml_predictions"
    __table_args__ = (
        UniqueConstraint("model_id", "signal_id", name="uq_ml_prediction"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_models.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[int] = mapped_column(BigInteger, index=True)
    figi: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(8))
    probability: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(16), default="")
