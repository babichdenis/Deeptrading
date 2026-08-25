from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionState(str, Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionAction(str, Enum):
    NO_SIGNAL = "NO_SIGNAL"
    WARMUP = "WARMUP"
    ACCEPT_ENTRY = "ACCEPT_ENTRY"
    ACCEPT_EXIT = "ACCEPT_EXIT"
    IGNORE_SAME_SIDE = "IGNORE_SAME_SIDE"
    REJECT_MIN_HOLD = "REJECT_MIN_HOLD"
    REJECT_SESSION_CUTOFF = "REJECT_SESSION_CUTOFF"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TARGET = "target"
    SIGNAL_EXIT = "signal_exit"
    SESSION_CLOSE = "session_close"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    side: Side
    time: datetime
    reason: str = ""
    features: dict[str, float] = field(default_factory=dict)
    kind: str = "entry"  # "entry" | "exit"


@dataclass(frozen=True)
class ExitPlan:
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class Position:
    figi: str
    state: PositionState
    qty: int
    entry_time: datetime
    entry_index: int
    entry_price: float
    initial_stop: float | None
    target: float | None
    bars_held: int = 0
    entry_commission: float = 0.0
    entry_slippage: float = 0.0


@dataclass(frozen=True)
class Trade:
    trade_id: str
    figi: str
    side: str
    qty: int
    entry_index: int
    entry_time: datetime
    entry_price: float
    exit_index: int
    exit_time: datetime
    exit_price: float
    bars_held: int
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    exit_reason: str
    initial_stop: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class AuditEntry:
    index: int
    time: datetime
    kind: str
    detail: str
