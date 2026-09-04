from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    figi: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    class_code: Mapped[str] = mapped_column(String(32))
    isin: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(512))
    currency: Mapped[str] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(String(128))
    lot: Mapped[int] = mapped_column(Integer)
    long_lev: Mapped[float] = mapped_column(Float, default=0.0)
    short_lev: Mapped[float] = mapped_column(Float, default=0.0)
    long_lev_client: Mapped[float] = mapped_column(Float, default=0.0)
    short_lev_client: Mapped[float] = mapped_column(Float, default=0.0)
    dlong: Mapped[float] = mapped_column(Float, default=0.0)
    dshort: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
