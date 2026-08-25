from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    initial_cash: Mapped[float] = mapped_column(Numeric(20, 2), default=100000)
    cash: Mapped[float] = mapped_column(Numeric(20, 2), default=100000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (UniqueConstraint("account_id", "figi", name="uq_paper_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"))
    figi: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Numeric(20, 6))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True)
    figi: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Numeric(20, 6))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float] = mapped_column(Numeric(20, 6))
    gross_pnl: Mapped[float] = mapped_column(Numeric(20, 6))
    commission: Mapped[float] = mapped_column(Numeric(20, 6))
    slippage: Mapped[float] = mapped_column(Numeric(20, 6))
    net_pnl: Mapped[float] = mapped_column(Numeric(20, 6))
    exit_reason: Mapped[str] = mapped_column(String(32))
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
