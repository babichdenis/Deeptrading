"""Signal Trace (фаза 1): runs / events / outcomes. См. DECISIONS.md 2026-10-01 17:30.

Append-only журнал жизненного цикла сигнала. Пишет SignalTraceEmitter через
SqlTraceWriter (батчи, ON CONFLICT DO NOTHING); hot path в БД не ходит.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SignalTraceRun(Base):
    __tablename__ = "signal_trace_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_key: Mapped[str] = mapped_column(String(160), index=True)
    contour: Mapped[str] = mapped_column(String(16), default="runtime")
    mode: Mapped[str] = mapped_column(String(16), default="")
    feed: Mapped[str] = mapped_column(String(16), default="")
    test_name: Mapped[str] = mapped_column(String(160), default="")
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    interval: Mapped[str] = mapped_column(String(16), default="")
    replay_from: Mapped[str] = mapped_column(String(40), default="")
    replay_to: Mapped[str] = mapped_column(String(40), default="")
    config_hash: Mapped[str] = mapped_column(String(24), default="")
    preset_hash: Mapped[str] = mapped_column(String(24), default="")
    dataset_version: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SignalTraceEvent(Base):
    __tablename__ = "signal_trace_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_signal_trace_events_run_seq"),
        Index("ix_signal_trace_events_run_ts", "run_id", "ts_bar"),
        Index("ix_signal_trace_events_signal", "signal_id"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts_bar: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ts_wall: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    figi: Mapped[str] = mapped_column(String(24), default="")
    ticker: Mapped[str] = mapped_column(String(16), default="")
    stage: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[str] = mapped_column(String(8), default="")
    kind: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    reason_code: Mapped[str] = mapped_column(String(40), default="")
    action: Mapped[str] = mapped_column(String(24), default="")
    order_id: Mapped[str] = mapped_column(String(32), default="")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SignalTraceOutcome(Base):
    __tablename__ = "signal_trace_outcomes"

    signal_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    outcome_cfg_hash: Mapped[str] = mapped_column(String(24), primary_key=True)
    anchor: Mapped[str] = mapped_column(String(16), default="next_open")
    anchor_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    horizon_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    future_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    hit_tp_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hit_sl_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label: Mapped[str | None] = mapped_column(String(24), nullable=True)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
