"""Event bus для market data — подписка на события свечей.

CandleHub генерирует события, стратегии и индикаторы подписываются на них.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from app.marketdata.models import CandleEvent, MarketDataEvent

logger = logging.getLogger("candlehub.events")


@dataclass
class EventBus:
    """Шина событий для market data.

    Подписчики регистрируются на типы событий. События доставляются
    синхронно (без очереди) — для простоты и предсказуемости.
    """

    _candle_handlers: dict[str, list[Callable]] = field(default_factory=lambda: defaultdict(list))
    _market_handlers: dict[str, list[Callable]] = field(default_factory=lambda: defaultdict(list))

    def on_candle(self, event_type: str, handler: Callable) -> None:
        """Подписаться на событие свечи (CANDLE_CLOSED, CANDLE_FORMING и т.д.)."""
        self._candle_handlers[event_type].append(handler)
        logger.debug("EventBus: handler %s subscribed to %s", handler.__name__, event_type)

    def off_candle(self, event_type: str, handler: Callable) -> None:
        """Отписаться от события свечи."""
        if handler in self._candle_handlers[event_type]:
            self._candle_handlers[event_type].remove(handler)

    def on_market(self, event_type: str, handler: Callable) -> None:
        """Подписаться на рыночное событие (STREAM_CONNECTED и т.д.)."""
        self._market_handlers[event_type].append(handler)

    def emit_candle(self, event: CandleEvent) -> None:
        """Отправить событие свечи всем подписчикам."""
        handlers = self._candle_handlers.get(event.state, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error("EventBus: handler %s failed on %s: %s",
                             handler.__name__, event.state, e, exc_info=True)

    def emit_market(self, event: MarketDataEvent) -> None:
        """Отправить рыночное событие всем подписчикам."""
        handlers = self._market_handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error("EventBus: handler %s failed on %s: %s",
                             handler.__name__, event.event_type, e, exc_info=True)
