from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# BIGINT-автоинкремент есть только в Postgres; в SQLite (тесты) автоинкрементится
# исключительно INTEGER PRIMARY KEY — иначе вставка падает по NOT NULL на id.
_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class ReportRun(Base):
    __tablename__ = "report_runs"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_report_runs_content_hash"),
        Index("ix_report_runs_kind_created", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String(256), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="real")
    name: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportRow(Base):
    __tablename__ = "report_rows"
    __table_args__ = (
        Index("ix_report_rows_run_strategy", "run_id", "strategy"),
        Index("ix_report_rows_run_ticker", "run_id", "ticker"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("report_runs.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(256), default="")
    exit: Mapped[str] = mapped_column(String(32), default="")
    ticker: Mapped[str] = mapped_column(String(32), default="")
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    gw: Mapped[float] = mapped_column(Float, default=0.0)
    gl: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    pf: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_dd_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ReportTrade(Base):
    __tablename__ = "report_trades"
    __table_args__ = (
        Index("ix_report_trades_run_strategy", "run_id", "strategy"),
        Index("ix_report_trades_run_ticker", "run_id", "ticker"),
        Index("ix_report_trades_run_entry", "run_id", "entry_time"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("report_runs.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(256), default="")
    exit: Mapped[str] = mapped_column(String(32), default="")
    ticker: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8), default="LONG")
    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bars_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    sl_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    session: Mapped[str | None] = mapped_column(String(32), nullable=True)
    regime_adx: Mapped[str | None] = mapped_column(String(16), nullable=True)
    er_in: Mapped[float | None] = mapped_column(Float, nullable=True)


class ReportSlice(Base):
    __tablename__ = "report_slices"
    __table_args__ = (
        UniqueConstraint("run_id", "strategy", "dim", "bucket", name="uq_report_slices_cell"),
        Index("ix_report_slices_run_dim", "run_id", "dim"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("report_runs.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(256), default="")
    dim: Mapped[str] = mapped_column(String(16), default="")
    bucket: Mapped[str] = mapped_column(String(64), default="")
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    gw: Mapped[float] = mapped_column(Float, default=0.0)
    gl: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    gross_wins_n: Mapped[int] = mapped_column(Integer, default=0)
    gross_losses_n: Mapped[int] = mapped_column(Integer, default=0)
