import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StrategyRun(Base):
    __tablename__ = "strategy_runs"
    __table_args__ = (
        UniqueConstraint(
            "figi", "interval_name", "strategy_id", "params_hash", "engine_version",
            name="uq_strategy_run_params",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    figi: Mapped[str] = mapped_column(String(32))
    interval_name: Mapped[str] = mapped_column(String(16))
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(16))
    engine_version: Mapped[str] = mapped_column(String(32), default="trade_engine_v1")
    data_version: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    params_hash: Mapped[str] = mapped_column(String(16))
    bars: Mapped[int] = mapped_column(Integer)
    from_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    to_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategySignal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("run_id", "ts", "side", name="uq_signal_run_ts_side"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_runs.id", ondelete="CASCADE"), index=True
    )
    figi: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)


class SignalDecision(Base):
    __tablename__ = "signal_decisions"
    __table_args__ = (
        UniqueConstraint("signal_id", "policy_id", "params_hash", name="uq_signal_decision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    policy_id: Mapped[str] = mapped_column(String(64))
    params_hash: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class RunDependency(Base):
    __tablename__ = "run_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_runs.id", ondelete="CASCADE"), index=True
    )
    child_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_runs.id")
    )
    role: Mapped[str] = mapped_column(String(32))
