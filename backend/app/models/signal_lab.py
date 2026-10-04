"""Signal Lab: сырые сигналы и независимые симуляции (research-слой).

Решение архитектора 2026-10-02 (DECISIONS.md): сигнал генерируется один раз,
выходы (fixed SL/TP, trailing) и исходы привязываются к нему отдельными строками.
Существующие signals/strategy_runs/signal_trace_* не трогаются.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    JSON, BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LabExperimentRun(Base):
    """Один прогон Signal Lab (config_hash — ключ идемпотентности)."""
    __tablename__ = "lab_experiment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_key: Mapped[str] = mapped_column(String(128), index=True)
    config_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    stage: Mapped[str] = mapped_column(String(16), default="discovery")
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    dataset_key: Mapped[str] = mapped_column(String(64), default="moex_1m")
    dataset_version: Mapped[str] = mapped_column(String(64), default="")
    period_from: Mapped[str] = mapped_column(String(40), default="")
    period_to: Mapped[str] = mapped_column(String(40), default="")
    warmup_from: Mapped[str] = mapped_column(String(40), default="")
    universe: Mapped[dict] = mapped_column(JSON, default=list)
    source_tf: Mapped[str] = mapped_column(String(8), default="1min")
    generated_tfs: Mapped[dict] = mapped_column(JSON, default=list)
    engines: Mapped[dict] = mapped_column(JSON, default=list)
    exit_profiles: Mapped[dict] = mapped_column(JSON, default=list)
    trailing_profiles: Mapped[dict] = mapped_column(JSON, default=list)
    horizons: Mapped[dict] = mapped_column(JSON, default=list)
    timeout_bars: Mapped[int] = mapped_column(Integer, default=24)
    costs: Mapped[dict] = mapped_column(JSON, default=dict)
    session_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    code_version: Mapped[str] = mapped_column(String(64), default="")
    engine_version: Mapped[str] = mapped_column(String(32), default="trade_engine_v1")
    cost_model_version: Mapped[str] = mapped_column(String(32), default="canonical_v1")
    regime_version: Mapped[str] = mapped_column(String(16), default="v2.0")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class LabSignalEvent(Base):
    """Сырой сигнал движка: что сказала стратегия БЕЗ гейтов и позиций."""
    __tablename__ = "lab_signal_events"
    __table_args__ = (
        UniqueConstraint("run_id", "figi", "strategy_id", "tf", "bar_ts", "side", "kind",
                         name="uq_lab_signal_event"),
        Index("ix_lab_signals_run_tf", "run_id", "tf"),
        Index("ix_lab_signals_run_strategy", "run_id", "strategy_id"),
        Index("ix_lab_signals_figi_bar", "figi", "bar_ts"),
        Index("ix_lab_signals_uid", "signal_uid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("lab_experiment_runs.id", ondelete="CASCADE"))
    signal_uid: Mapped[str] = mapped_column(String(32), default="")
    figi: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(32), default="")
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32), default="")
    params_hash: Mapped[str] = mapped_column(String(32), default="")
    tf: Mapped[str] = mapped_column(String(8))
    tf_seconds: Mapped[int] = mapped_column(Integer, default=60)
    bar_ts: Mapped[str] = mapped_column(String(40))
    bar_close_ts: Mapped[str] = mapped_column(String(40), default="")
    side: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(8), default="entry")
    reason: Mapped[str] = mapped_column(String(256), default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    anchor_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entry_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    anchor_gap_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    atr_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime_obs_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    regime_direction: Mapped[str | None] = mapped_column(String(24), nullable=True)
    regime_structure: Mapped[str | None] = mapped_column(String(24), nullable=True)
    regime_volatility: Mapped[str | None] = mapped_column(String(24), nullable=True)
    regime_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class LabSignalEventRun(Base):
    """Событие = непрерывная серия одинаковых сигналов (figi × tf × engine).

    Сырые сигналы (lab_signal_events) остаются как есть; этот слой нужен, чтобы
    не считать «каждый бар состояния» отдельным входом (плотность 1.00 у половины
    движков искажала все средние). Новый event при смене стороны, появлении после
    паузы (разрыв последовательности) или старте. canonical entry = start_ts.
    """
    __tablename__ = "lab_signal_event_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "figi", "strategy_id", "tf", "start_ts",
                         name="uq_lab_event"),
        Index("ix_lab_events_run_engine_tf", "run_id", "strategy_id", "tf"),
        Index("ix_lab_events_figi_start", "figi", "start_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("lab_experiment_runs.id", ondelete="CASCADE"))
    figi: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(32), default="")
    strategy_id: Mapped[str] = mapped_column(String(64))
    tf: Mapped[str] = mapped_column(String(8))
    tf_seconds: Mapped[int] = mapped_column(Integer, default=60)
    side: Mapped[str] = mapped_column(String(8))
    start_ts: Mapped[str] = mapped_column(String(40))
    start_close_ts: Mapped[str] = mapped_column(String(40), default="")
    end_ts: Mapped[str] = mapped_column(String(40), default="")
    duration_bars: Mapped[int] = mapped_column(Integer, default=1)
    signal_count: Mapped[int] = mapped_column(Integer, default=1)
    start_signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lab_signal_events.id", ondelete="CASCADE"))
    created_at: Mapped[str] = mapped_column(String(40), default="")


class LabMarketOutcome(Base):
    """Path-independent исход горизонта: future_return и MFE/MAE по стороне."""
    __tablename__ = "lab_market_outcomes"
    __table_args__ = (
        UniqueConstraint("signal_id", "horizon_bars", "outcome_cfg_hash", name="uq_lab_outcome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lab_signal_events.id", ondelete="CASCADE"), index=True)
    horizon_bars: Mapped[int] = mapped_column(Integer)
    horizon_minutes: Mapped[int] = mapped_column(Integer, default=0)
    anchor_ts: Mapped[str] = mapped_column(String(40), default="")
    entry_px: Mapped[float] = mapped_column(Float, default=0.0)
    end_ts: Mapped[str] = mapped_column(String(40), default="")
    bars_1m: Mapped[int] = mapped_column(Integer, default=0)
    future_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    future_return_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mae_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    t_mfe_bars_1m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    t_mae_bars_1m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atr_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_period: Mapped[int] = mapped_column(Integer, default=14)
    outcome_cfg_hash: Mapped[str] = mapped_column(String(16), default="")


class LabFixedExit(Base):
    """Результат фиксированного SL/TP (4 профиля ATR)."""
    __tablename__ = "lab_fixed_exits"
    __table_args__ = (
        UniqueConstraint("signal_id", "profile_code", "cfg_hash", name="uq_lab_fixed_exit"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lab_signal_events.id", ondelete="CASCADE"), index=True)
    profile_code: Mapped[str] = mapped_column(String(24))
    sl_atr: Mapped[float] = mapped_column(Float, default=0.0)
    tp_atr: Mapped[float] = mapped_column(Float, default=0.0)
    atr_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_period: Mapped[int] = mapped_column(Integer, default=14)
    entry_ts: Mapped[str] = mapped_column(String(40), default="")
    entry_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exit_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(24), default="")
    bars_held_1m: Mapped[int] = mapped_column(Integer, default=0)
    bars_held_tf: Mapped[int] = mapped_column(Integer, default=0)
    gross_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_first_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    gap_open: Mapped[bool] = mapped_column(Boolean, default=False)
    cfg_hash: Mapped[str] = mapped_column(String(16), default="")


class LabTrailingResult(Base):
    """Результат одной модели трейлинга/выхода на конкретном сигнале."""
    __tablename__ = "lab_trailing_results"
    __table_args__ = (
        UniqueConstraint("signal_id", "model_code", "model_cfg_hash", name="uq_lab_trailing"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lab_signal_events.id", ondelete="CASCADE"), index=True)
    model_code: Mapped[str] = mapped_column(String(32))
    policy_id: Mapped[str] = mapped_column(String(32), default="")
    policy_version: Mapped[str] = mapped_column(String(16), default="")
    model_cfg_hash: Mapped[str] = mapped_column(String(16), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    entry_ts: Mapped[str] = mapped_column(String(40), default="")
    entry_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    activated: Mapped[bool] = mapped_column(Boolean, default=False)
    activate_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    trail_stop_final: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    exit_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(24), default="")
    bars_held_1m: Mapped[int] = mapped_column(Integer, default=0)
    bars_held_tf: Mapped[int] = mapped_column(Integer, default=0)
    gross_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)


class LabRegimeObservation(Base):
    """Персист Regime V2 (descriptive-слой; в runtime не подключён)."""
    __tablename__ = "lab_regime_observations"
    __table_args__ = (
        UniqueConstraint("figi", "tf_seconds", "obs_ts", "version", "params_hash",
                         name="uq_lab_regime_obs"),
        Index("ix_lab_regime_figi_ts", "figi", "tf_seconds", "obs_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    figi: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(32), default="")
    tf_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    obs_ts: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(16), default="v2.0")
    params_hash: Mapped[str] = mapped_column(String(16), default="")
    direction: Mapped[str | None] = mapped_column(String(24), nullable=True)
    direction_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility: Mapped[str | None] = mapped_column(String(24), nullable=True)
    volatility_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    structure: Mapped[str | None] = mapped_column(String(24), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason_codes: Mapped[dict] = mapped_column(JSON, default=list)
    measurements: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default="")
