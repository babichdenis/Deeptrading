"""IndicatorHub — stateful индикаторы с кэшем и двумя фазами (preview/commit).

Индикатор получает candle.update() один раз. Внутреннее состояние
(avg_gain, avg_loss, prev_close и т.д.) хранится в объекте индикатора.

Две фазы:
- preview(forming_candle) — предварительный расчёт (не коммитит состояние)
- commit(closed_candle) — фиксация состояния (вызывается один раз на закрытый бар)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.marketdata.models import Candle, IndicatorKey

logger = logging.getLogger("candlehub.indicators")


class Indicator(ABC):
    """Базовый класс индикатора."""

    def __init__(self, params: tuple[int | float, ...] = ()):
        self.params = params
        self._state: dict[str, Any] = {}

    @abstractmethod
    def preview(self, candle: Candle) -> float | None:
        """Предварительный расчёт (forming-бар, не коммитит состояние)."""
        ...

    @abstractmethod
    def commit(self, candle: Candle) -> float | None:
        """Фиксация расчёта (closed-бар, коммитит состояние)."""
        ...

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)


class RSIIndicator(Indicator):
    """RSI (Relative Strength Index) — Wilder smoothing."""

    def __init__(self, period: int = 14):
        super().__init__((period,))
        self.period = period
        self._state = {
            "avg_gain": 0.0,
            "avg_loss": 0.0,
            "prev_close": None,
            "count": 0,
        }

    def preview(self, candle: Candle) -> float | None:
        """Предварительный RSI (без коммита состояния)."""
        if self._state["prev_close"] is None:
            return None
        change = candle.close - self._state["prev_close"]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (self._state["avg_gain"] * (self.period - 1) + gain) / self.period
        avg_loss = (self._state["avg_loss"] * (self.period - 1) + loss) / self.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def commit(self, candle: Candle) -> float | None:
        """Фиксация RSI (коммитит состояние)."""
        if self._state["prev_close"] is None:
            self._state["prev_close"] = candle.close
            self._state["count"] += 1
            return None

        change = candle.close - self._state["prev_close"]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._state["count"] < self.period:
            self._state["avg_gain"] += gain / self.period
            self._state["avg_loss"] += loss / self.period
        else:
            self._state["avg_gain"] = (self._state["avg_gain"] * (self.period - 1) + gain) / self.period
            self._state["avg_loss"] = (self._state["avg_loss"] * (self.period - 1) + loss) / self.period

        self._state["prev_close"] = candle.close
        self._state["count"] += 1

        if self._state["avg_loss"] == 0:
            return 100.0
        rs = self._state["avg_gain"] / self._state["avg_loss"]
        return 100.0 - 100.0 / (1.0 + rs)


class EMAIndicator(Indicator):
    """EMA (Exponential Moving Average)."""

    def __init__(self, period: int = 20):
        super().__init__((period,))
        self.period = period
        self._state = {"ema": None, "count": 0}

    def preview(self, candle: Candle) -> float | None:
        if self._state["ema"] is None:
            return candle.close
        k = 2.0 / (self.period + 1)
        return candle.close * k + self._state["ema"] * (1 - k)

    def commit(self, candle: Candle) -> float | None:
        if self._state["ema"] is None:
            self._state["ema"] = candle.close
            self._state["count"] += 1
            return self._state["ema"]
        k = 2.0 / (self.period + 1)
        self._state["ema"] = candle.close * k + self._state["ema"] * (1 - k)
        self._state["count"] += 1
        return self._state["ema"]


class ATRIndicator(Indicator):
    """ATR (Average True Range) — Wilder smoothing."""

    def __init__(self, period: int = 14):
        super().__init__((period,))
        self.period = period
        self._state = {"atr": None, "prev_close": None, "trs": []}

    def preview(self, candle: Candle) -> float | None:
        if self._state["prev_close"] is None:
            return None
        tr = max(
            candle.high - candle.low,
            abs(candle.high - self._state["prev_close"]),
            abs(candle.low - self._state["prev_close"]),
        )
        if self._state["atr"] is None:
            return tr
        return (self._state["atr"] * (self.period - 1) + tr) / self.period

    def commit(self, candle: Candle) -> float | None:
        if self._state["prev_close"] is None:
            self._state["prev_close"] = candle.close
            return None
        tr = max(
            candle.high - candle.low,
            abs(candle.high - self._state["prev_close"]),
            abs(candle.low - self._state["prev_close"]),
        )
        self._state["trs"].append(tr)
        if len(self._state["trs"]) > self.period:
            self._state["trs"].pop(0)
        if self._state["atr"] is None:
            self._state["atr"] = sum(self._state["trs"]) / len(self._state["trs"])
        else:
            self._state["atr"] = (self._state["atr"] * (self.period - 1) + tr) / self.period
        self._state["prev_close"] = candle.close
        return self._state["atr"]


class IndicatorHub:
    """Кэш индикаторов: один индикатор на (figi, timeframe, indicator, params).

    Если 7 стратегий используют EMA20, она считается один раз.
    """

    def __init__(self):
        self._indicators: dict[IndicatorKey, Indicator] = {}

    def get(self, key: IndicatorKey) -> Indicator:
        """Получить или создать индикатор по ключу."""
        if key not in self._indicators:
            self._indicators[key] = self._create(key)
        return self._indicators[key]

    def _create(self, key: IndicatorKey) -> Indicator:
        """Создать индикатор по ключу."""
        name = key.indicator.lower()
        params = key.params
        if name == "rsi":
            return RSIIndicator(int(params[0]) if params else 14)
        elif name == "ema":
            return EMAIndicator(int(params[0]) if params else 20)
        elif name == "atr":
            return ATRIndicator(int(params[0]) if params else 14)
        else:
            raise ValueError(f"unknown indicator: {name}")

    def preview(self, key: IndicatorKey, candle: Candle) -> float | None:
        """Предварительный расчёт (forming)."""
        return self.get(key).preview(candle)

    def commit(self, key: IndicatorKey, candle: Candle) -> float | None:
        """Фиксация расчёта (closed)."""
        return self.get(key).commit(candle)

    def clear(self) -> None:
        """Очистить кэш."""
        self._indicators.clear()
