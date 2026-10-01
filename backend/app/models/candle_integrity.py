"""Watchdog целостности свечей: дневные контрольные суммы (см. candle_integrity.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class CandleIntegrity(Base):
    __tablename__ = "candle_integrity"
    __table_args__ = (
        UniqueConstraint("figi", "interval", "day", name="uq_candle_integrity_figi_interval_day"),
        Index("ix_candle_integrity_day", "day"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    figi: Mapped[str] = mapped_column(String(32))
    interval: Mapped[int] = mapped_column(Integer)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    rows: Mapped[int] = mapped_column(Integer, default=0)
    min_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hash_xor: Mapped[int | None] = mapped_column(Numeric(30, 0), nullable=True)
    hash_sum: Mapped[Any | None] = mapped_column(Numeric(40, 0), nullable=True)
    volume_sum: Mapped[Any | None] = mapped_column(Numeric(40, 8), nullable=True)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
