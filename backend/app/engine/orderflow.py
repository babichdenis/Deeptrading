from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class OrderAction(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    FLIP = "FLIP"
    MODIFY_STOP = "MODIFY_STOP"
    MODIFY_TARGET = "MODIFY_TARGET"
    CANCEL_ORDER = "CANCEL_ORDER"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_CHECKED = "RISK_CHECKED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}


class RiskDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    PAUSE = "PAUSE"


class PositionEventType(str, Enum):
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    SAME_SIDE_IGNORED = "SAME_SIDE_IGNORED"
    OPPOSITE_PENDING = "OPPOSITE_PENDING"
    OPPOSITE_PENDING_CANCELLED = "OPPOSITE_PENDING_CANCELLED"
    TRAIL_ACTIVATED = "TRAIL_ACTIVATED"
    STOP_UPDATED = "STOP_UPDATED"
    TARGET_UPDATED = "TARGET_UPDATED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ExitReasonCode(str, Enum):
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    ATR_TRAIL_STOP = "ATR_TRAIL_STOP"
    OPPOSITE_SIGNAL_EXIT = "OPPOSITE_SIGNAL_EXIT"
    CONFIRMED_OPPOSITE_FLIP = "CONFIRMED_OPPOSITE_FLIP"
    SESSION_CLOSE = "SESSION_CLOSE"
    TIME_STOP = "TIME_STOP"
    RISK_EXIT = "RISK_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    DATA_GAP_EXIT = "DATA_GAP_EXIT"
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    figi: str
    action: OrderAction
    side: str
    requested_qty: int
    source_signal_id: str | None = None
    source_decision_id: str | None = None
    position_state: str = "FLAT"
    execution_rule: str = "NEXT_AVAILABLE_OPEN"
    config_version: str = ""
    idempotency_key: str = ""
    created_at: datetime | None = None
    status: str = "CREATED"


@dataclass
class Order:
    order_id: str
    intent_id: str
    client_order_id: str
    figi: str
    side: str
    purpose: str
    order_type: str = "MARKET"
    requested_qty: int = 1
    filled_qty: int = 0
    remaining_qty: int = 1
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    figi: str
    side: str
    quantity: int
    price: float
    commission: float = 0.0
    slippage: float = 0.0
    fill_time: datetime | None = None
    broker_fill_id: str | None = None


@dataclass(frozen=True)
class Decision:
    decision_id: str
    figi: str
    action: str
    reason_code: str
    source_signal_id: str | None = None
    position_state: str = "FLAT"
    created_at: datetime | None = None
    checks: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionEvent:
    event_id: str
    figi: str
    event_type: PositionEventType
    time: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskCheckResult:
    decision: RiskDecision
    reason_code: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


def make_idempotency_key(config_version: str, figi: str, ts: str, action: str, side: str) -> str:
    return f"{config_version}:{figi}:{ts}:{action}:{side}"


def make_client_order_id(bot_id: str, config_version: str, intent_id: str) -> str:
    return f"{bot_id}-{config_version}-{intent_id}"
