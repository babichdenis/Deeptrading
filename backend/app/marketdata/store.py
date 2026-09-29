"""CandleStore — единый доступ к свечам (closed/forming).

Стратегии не работают напрямую с deque/списками. Они получают данные
через CandleStore с чётким разделением:
- closed — завершённые бары (никогда не меняются)
- forming — текущий незакрытый бар (обновляется)
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime

from app.marketdata.models import Candle, CandleState

logger = logging.getLogger("candlehub.store")


class CandleStore:
    """Хранилище свечей для одной подписки (figi, timeframe).

    closed: deque закрытых баров (maxlen ограничение)
    forming: текущий незакрытый бар (или None)
    """

    def __init__(self, maxlen: int = 5000):
        self.maxlen = maxlen
        self._closed: deque[Candle] = deque(maxlen=maxlen)
        self._forming: Candle | None = None
        self._last_ts: datetime | None = None

    # --- API для чтения --------------------------------------------------

    def get(self, n: int | None = None) -> list[Candle]:
        """Последние n закрытых баров (или все, если n=None)."""
        if n is None:
            return list(self._closed)
        return list(self._closed)[-n:]

    def last(self) -> Candle | None:
        """Последний закрытый бар (или None)."""
        return self._closed[-1] if self._closed else None

    def closed(self, n: int | None = None) -> list[Candle]:
        """Закрытые бары (алиас для get)."""
        return self.get(n)

    def forming(self) -> Candle | None:
        """Текущий формирующийся бар (или None)."""
        return self._forming

    def all(self) -> list[Candle]:
        """Закрытые + forming (для отображения в UI)."""
        if self._forming is not None:
            return [*self._closed, self._forming]
        return list(self._closed)

    # --- API для записи --------------------------------------------------

    def append(self, candle: Candle) -> None:
        """Добавить закрытую свечу в историю."""
        self._closed.append(candle)
        self._last_ts = candle.ts

    def update_forming(self, candle: Candle) -> None:
        """Обновить формирующийся бар (замена, не мутация)."""
        self._forming = candle

    def close(self, candle: Candle) -> Candle:
        """Закрыть forming-бар и добавить его в историю.

        Возвращает закрытую свечу.
        """
        closed_candle = Candle(
            ts=candle.ts,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            figi=candle.figi,
            timeframe=candle.timeframe,
            source=candle.source,
            sequence=candle.sequence,
            state=CandleState.CLOSED,
        )
        self._closed.append(closed_candle)
        self._forming = None
        self._last_ts = closed_candle.ts
        return closed_candle

    def clear(self) -> None:
        """Очистить хранилище."""
        self._closed.clear()
        self._forming = None
        self._last_ts = None

    # --- диагностика -----------------------------------------------------

    @property
    def last_ts(self) -> datetime | None:
        return self._last_ts

    def __len__(self) -> int:
        return len(self._closed)
