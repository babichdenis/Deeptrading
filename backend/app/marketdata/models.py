"""Market data models — единый источник истины для свечей.

CandleHub не знает, какая стратегия торгует. Он только управляет жизненным
циклом свечи: normalize → deduplicate → ordering → closed/forming → gap detection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class CandleState(str, Enum):
    FORMING = "FORMING"
    CLOSED = "CLOSED"


class FeedStatus(str, Enum):
    REQUESTED = "REQUESTED"
    SUBSCRIBING = "SUBSCRIBING"
    SUBSCRIBED = "SUBSCRIBED"
    WARMING_UP = "WARMING_UP"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    REMOVING = "REMOVING"
    REMOVED = "REMOVED"


@dataclass(frozen=True)
class Candle:
    """Нормализованная свеча (единый формат для всех источников)."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    figi: str = ""
    timeframe: str = "1m"
    source: Literal["stream", "polling", "history", "replay"] = "stream"
    sequence: int = 0
    state: CandleState = CandleState.FORMING


@dataclass(frozen=True)
class Symbol:
    """Инструмент (акция, индекс, валюта)."""
    figi: str
    symbol: str = ""
    name: str = ""
    lot: int = 1


@dataclass(frozen=True)
class Subscription:
    """Подписка на данные по инструменту и таймфрейму."""
    figi: str
    timeframe: str
    closed_only: bool = True
    indicators: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndicatorKey:
    """Ключ индикатора: (figi, timeframe, indicator, params)."""
    figi: str
    timeframe: str
    indicator: str
    params: tuple[int | float, ...] = ()


@dataclass
class SubscriptionState:
    """Состояние подписки (runtime)."""
    subscription: Subscription
    status: FeedStatus = FeedStatus.REQUESTED
    sequence: int = 0
    last_ts: datetime | None = None
    warmup_bars: int = 0
    error: str | None = None


@dataclass(frozen=True)
class CandleEvent:
    """Событие жизненного цикла свечи."""
    event_id: str
    symbol: str
    figi: str
    timeframe: str
    ts: datetime
    candle: Candle
    state: Literal["FORMING", "CLOSED"]
    source: Literal["stream", "polling", "history", "replay"]
    sequence: int
    is_new: bool = True
    is_correction: bool = False


@dataclass(frozen=True)
class MarketDataEvent:
    """Срытие рыночных данных (не связано с конкретной свечой)."""
    event_type: str
    figi: str
    timeframe: str
    ts: datetime
    details: dict = field(default_factory=dict)


REASON_CODES = frozenset({
    "CANDLE_RECEIVED",
    "CANDLE_DUPLICATE",
    "CANDLE_OUT_OF_ORDER",
    "CANDLE_GAP",
    "CANDLE_CORRECTED",
    "CANDLE_CLOSED",
    "CANDLE_FORMING",
    "SUBSCRIPTION_ADDED",
    "SUBSCRIPTION_FAILED",
    "STREAM_CONNECTED",
    "STREAM_DISCONNECTED",
    "STREAM_FALLBACK_POLLING",
    "PERSIST_OK",
    "PERSIST_FAILED",
    "INDICATOR_UPDATED",
    "INDICATOR_ERROR",
    "SYMBOL_ADD_REQUEST",
    "SYMBOL_ADDED",
    "SUBSCRIPTION_REQUEST",
    "SUBSCRIPTION_CONFIRMED",
    "HISTORY_WARMUP_START",
    "HISTORY_WARMUP_DONE",
    "LIVE_READY",
})
