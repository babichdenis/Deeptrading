from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiDecision(Base):
    """Решение AI-гейта (вход) + исход (контрфакт/факт), считается трекером.

    saved_rub/missed_rub — «сэкономил/упустил» относительно контрфакта:
    для отклонённых заявок считаем, что было бы, если бы вошли (цена +30 мин);
    для одобренных — фактический net_pnl сделки (actual_pnl).
    """
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    order_id: Mapped[str] = mapped_column(String(64), default="")
    figi: Mapped[str] = mapped_column(String(40), default="", index=True)
    ticker: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(48), default="")
    decision: Mapped[str] = mapped_column(String(12), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    advice: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    agreement: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- исход (заполняет scripts/ai_gate_tracker.py) ---
    outcome_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    px_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    px_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    px_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_cf_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    saved_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    missed_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
