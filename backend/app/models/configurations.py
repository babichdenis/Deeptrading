import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Configuration(Base):
    """Конфигурация Warehouse: пакет стратегий + кворум + фильтры + правила выхода.

    Статусы: DRAFT → PREVIEW_RUNNING → PREVIEW_COMPLETED → READY_FOR_LAB
             → IN_LAB → LAB_COMPLETED (→ DESIGN_CANDIDATE / REJECTED)
    """

    __tablename__ = "configurations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)

    interval_name: Mapped[str] = mapped_column(String(16), default="hour")
    members: Mapped[list] = mapped_column(JSON, default=list)  # [{strategy_id, params}]
    quorum: Mapped[int] = mapped_column(Integer, default=1)

    exit_policy: Mapped[dict] = mapped_column(JSON, default=dict)  # {id, params}
    min_hold_bars: Mapped[int] = mapped_column(Integer, default=0)
    allow_short: Mapped[bool] = mapped_column(Boolean, default=False)
    session_policy: Mapped[dict] = mapped_column(JSON, default=dict)  # {entry_cutoff_bars, overnight}
    filters: Mapped[list] = mapped_column(JSON, default=list)  # v2: [{id, params}]

    preview_figi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_runs: Mapped[dict] = mapped_column(JSON, default=dict)  # {strategy_id: run_id}

    lab_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lab_experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
