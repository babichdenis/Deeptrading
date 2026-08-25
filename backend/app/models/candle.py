from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("figi", "interval", "ts", name="uq_candles_figi_interval_ts"),
        Index("ix_candles_figi_interval_ts", "figi", "interval", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    figi: Mapped[str] = mapped_column(String(32))
    interval: Mapped[int] = mapped_column(SmallInteger)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    volume: Mapped[int] = mapped_column(BigInteger)
