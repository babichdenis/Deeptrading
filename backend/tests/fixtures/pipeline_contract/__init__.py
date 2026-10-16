"""Обезличенные фикстуры сквозного контракта пайплайна (B3).

Пакет намеренно не импортирует ни `app.*`, ни `scripts/*` на уровне модуля:
импорт должен оставаться без I/O. Все помощники — чистые функции.
"""
from __future__ import annotations

from .dataset import (
    ANON_FIGI,
    ANON_TICKER,
    MSK,
    UTC,
    day_prices,
    last_weekdays,
    rows_from_prices,
    session_minutes,
    without,
)

__all__ = [
    "ANON_FIGI",
    "ANON_TICKER",
    "MSK",
    "UTC",
    "day_prices",
    "last_weekdays",
    "rows_from_prices",
    "session_minutes",
    "without",
]
