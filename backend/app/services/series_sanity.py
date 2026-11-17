"""Пригодность ценового ряда: разрывы масштаба и готовность к расчётам.

Зачем отдельный модуль. Контракт приёма ловит битый OHLC внутри бара, но не
видит ряд, где отдельный участок записан в другом масштабе цены. Живой пример
на 2026-11-15: у SNGS (`BBG0047315D0`) блок 2026-07-16 … 2026-08-24 записан в
масштабе ×100 (закрытия 5000–6483 при соседних 15.2), плюс одиночная строка
5908.5 за 2025-12-31. Ряд проходит все проверки OHLC и при этом непригоден:
ATR% по окну 44 бара выходит 310 % против медианы рынка 2.7 %, и такая бумага
встаёт первой в ранжировании отбора.

Правило простое: если внутри ряда закрытие меняется больше чем в `max_ratio`
раз за один шаг, ряд нельзя использовать как непрерывный, пока масштаб не
подтверждён источником. Это НЕ исправление истории: цены здесь не меняются,
модуль только говорит «ряд непригоден» и показывает, где именно.

Единицы и допуски не угадываются: разрыв описывается фактами (два закрытия и
их отношение), поэтому решение остаётся за тем, у кого есть данные источника.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Порог скачка. 3x за один бар — уже далеко за пределами рыночного движения
# (дивидендный гэп на MOEX редко превышает 30 %), и при этом не задевает
# законные низколиквидные инструменты с широким спредом.
DEFAULT_MAX_RATIO = 3.0


@dataclass(frozen=True)
class ScaleBreak:
    """Скачок уровня ряда: предыдущее и текущее закрытие плюс их отношение."""

    ts: datetime
    prev_close: float
    close: float

    @property
    def ratio(self) -> float:
        if self.prev_close <= 0:
            return float("inf")
        return self.close / self.prev_close

    def __str__(self) -> str:
        return (f"{self.ts.isoformat()}: {self.prev_close:g} -> {self.close:g} "
                f"(×{self.ratio:.4g})")


@dataclass(frozen=True)
class SeriesVerdict:
    """Итог проверки ряда. usable=False всегда сопровождается причиной."""

    usable: bool
    reason: str | None = None
    breaks: tuple[ScaleBreak, ...] = ()

    @property
    def first_break(self) -> ScaleBreak | None:
        return self.breaks[0] if self.breaks else None


def detect_scale_breaks(candles, *, max_ratio: float = DEFAULT_MAX_RATIO
                        ) -> tuple[ScaleBreak, ...]:
    """Найти скачки уровня: close меняется больше чем в max_ratio раз за шаг.

    Принимает любой объект с полями `ts` и `close` (EngineCandle, DB-строка,
    Candle). Нулевые и отрицательные закрытия пропускаются: это уже не разрыв
    масштаба, а испорченная цена, и её ловит контракт приёма.
    """
    if max_ratio <= 1.0:
        raise ValueError(f"max_ratio must be > 1, got {max_ratio}")

    out: list[ScaleBreak] = []
    prev: tuple[datetime, float] | None = None
    for c in candles:
        close = float(c.close)
        ts = c.ts
        if close <= 0:
            prev = None
            continue
        if prev is not None:
            prev_ts, prev_close = prev
            if prev_close > 0:
                ratio = close / prev_close
                if ratio > max_ratio or ratio < 1.0 / max_ratio:
                    out.append(ScaleBreak(ts=ts, prev_close=prev_close, close=close))
        prev = (ts, close)
    return tuple(out)


def series_usable(candles, *, max_ratio: float = DEFAULT_MAX_RATIO) -> SeriesVerdict:
    """Пригоден ли ряд как непрерывный. Не меняет и не фильтрует данные."""
    breaks = detect_scale_breaks(candles, max_ratio=max_ratio)
    if not breaks:
        return SeriesVerdict(usable=True, breaks=())
    first = breaks[0]
    reason = (f"scale_break at {first.ts.isoformat()}: "
              f"{first.prev_close:g} -> {first.close:g} "
              f"(×{first.ratio:.4g}, всего разрывов {len(breaks)})")
    return SeriesVerdict(usable=False, reason=reason, breaks=breaks)
