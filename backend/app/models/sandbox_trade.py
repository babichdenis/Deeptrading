from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SandboxTrade(Base):
    __tablename__ = "sandbox_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    figi: Mapped[str] = mapped_column(String(40), index=True)
    ticker: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8), default="LONG")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Numeric(20, 6))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    commission: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trailing_active: Mapped[bool] = mapped_column("trailing_active", default=False)
    meta: Mapped[str | None] = mapped_column("meta", Text, nullable=True)
    exit_meta: Mapped[str | None] = mapped_column("exit_meta", Text, nullable=True)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    mode: Mapped[str] = mapped_column(String(8), default="sandbox")  # sandbox | live | paper
