"""Signal Trace — контракты (P0). См. DECISIONS.md 2026-10-01 17:30.

Наблюдение за жизненным циклом сигнала: EVAL → RAW/ERROR → DECISION → ORDER
(→ FILL/EXIT — фаза 1.1). Чистый модуль без IO/БД: эмиттер и sink — в
app/services/signal_trace.py.

signal_id = sha256(run_id|contour|figi|strategy_id|version|interval|bar_ts|side|kind)[:32]
— стабильный логический корень для линии raw→decision→order→fill→trade→outcome.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

SCHEMA_VERSION = "signal_trace/0.1"


class Stage(str, Enum):
    RUN_OPEN = "RUN_OPEN"
    EVAL = "EVAL"
    RAW = "RAW"
    ERROR = "ERROR"
    DECISION = "DECISION"
    ORDER = "ORDER"
    FILL = "FILL"
    EXIT = "EXIT"
    RUN_CLOSE = "RUN_CLOSE"


class Status(str, Enum):
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


def make_signal_id(
    *,
    run_id: str,
    contour: str,
    figi: str,
    strategy_id: str,
    strategy_version: str,
    interval: str,
    bar_ts: datetime | None,
    side: str | None,
    kind: str | None,
) -> str:
    """Стабильный идентификатор сигнала (equality-ключ, не единственное представление)."""
    parts = "|".join(
        (
            str(run_id),
            str(contour),
            str(figi),
            str(strategy_id),
            str(strategy_version),
            str(interval),
            bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else str(bar_ts or ""),
            str(side or ""),
            str(kind or ""),
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


@dataclass
class RunInfo:
    """Паспорт прогона: обязателен для любых сравнений и ML-датасета."""

    run_id: str
    run_key: str
    contour: str = "runtime"
    mode: str = ""
    feed: str = ""
    test_name: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    interval: str = ""
    replay_from: str = ""
    replay_to: str = ""
    config_hash: str = ""
    preset_hash: str = ""
    dataset_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceEvent:
    """Одно событие журнала. Одна строка = одно событие (не «20 nullable-полей»)."""

    stage: Stage
    status: Status
    ts_bar: datetime | None = None
    figi: str = ""
    ticker: str = ""
    signal_id: str | None = None
    parent_event_id: str | None = None
    side: str | None = None
    kind: str | None = None
    reason: str | None = None
    reason_code: str | None = None
    action: str | None = None
    order_id: str | None = None
    price: float | None = None
    qty: int | None = None
    eval_ctx: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)


def event_id() -> str:
    return uuid.uuid4().hex
