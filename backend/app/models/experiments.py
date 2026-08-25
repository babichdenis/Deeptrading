import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("config_hash", name="uq_experiment_config"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    purpose: Mapped[str] = mapped_column(String(16), default="DESIGN")

    figi: Mapped[str] = mapped_column(String(32))
    interval_name: Mapped[str] = mapped_column(String(16))
    strategy_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_runs.id", ondelete="SET NULL"), index=True
    )
    strategy_id: Mapped[str] = mapped_column(String(64))

    signal_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    exit_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    session_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)

    engine_version: Mapped[str] = mapped_column(String(32))
    cost_model_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(16))

    data_version: Mapped[str] = mapped_column(String(16), default="")
    bars: Mapped[int] = mapped_column(Integer, default=0)
    from_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    to_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    research_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExperimentTrade(Base):
    __tablename__ = "experiment_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    trade_id: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    entry_index: Mapped[int] = mapped_column(Integer)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Numeric(20, 6))
    exit_index: Mapped[int] = mapped_column(Integer)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float] = mapped_column(Numeric(20, 6))
    bars_held: Mapped[int] = mapped_column(Integer)
    gross_pnl: Mapped[float] = mapped_column(Numeric(20, 6))
    commission: Mapped[float] = mapped_column(Numeric(20, 6))
    slippage: Mapped[float] = mapped_column(Numeric(20, 6))
    net_pnl: Mapped[float] = mapped_column(Numeric(20, 6))
    exit_reason: Mapped[str] = mapped_column(String(32))
    initial_stop: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
