from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, Integer, String, Text, UUID, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EnsembleRun(Base):
    """Очередь тестов ансамбля (вкладка Lab → Ансамбль).

    Статусы: QUEUED | RUNNING | DONE | FAILED | CANCELLED.
    params — параметры ансамбля (figis, days, bias_mode, entry_tf, cooldown, ...).
    progress — {"done", "total", "current", "by_stock": {ticker: {done,total}}}.
    result — {"by_stock": [{ticker, net, pf, trades, ...}], "total_net", ...}.
    """

    __tablename__ = "ensemble_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
