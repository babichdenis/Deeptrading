"""Domain-контракты слоя Universe → Screener.

Отсюда вниз по конвейеру: UniverseSnapshot → ScreenResult → ScreenedInstrument.
ATR, ранжирование, Top-N и веса здесь НЕ живут: Screener отвечает только
за допуск, признаки считает Feature-слой (app.bot.universe.features),
отбор — Selection (app.bot.universe.selection).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class UniverseSource(str, Enum):
    """Откуда Universe берёт кандидатов. Соответствует трём legacy-функциям."""

    ELIGIBLE_TABLE = "eligible_table"
    INSTRUMENT_INFO = "instrument_info"
    LIQUID_TICKERS = "liquid_tickers"


class ScreenReason(str, Enum):
    """Причины отсева. Список взят из реальных проверок app/bot/universe.py.

    Отдельные значения для сырых и ресемпленных баров нужны потому, что
    legacy-код отсеивал их двумя разными проверками на разных шагах.
    """

    NO_DATA = "no_data"
    INSUFFICIENT_BARS = "insufficient_bars"
    INSUFFICIENT_RESAMPLED_BARS = "insufficient_resampled_bars"
    INVALID_MARKET_DATA = "invalid_market_data"
    NOT_TRADEABLE = "not_tradeable"
    BLACKLISTED = "blacklisted"


@dataclass(frozen=True, order=True)
class InstrumentRef:
    """Однозначная идентичность инструмента.

    Порядок полей даёт стабильный tie-break (ticker ASC) для детерминизма
    snapshot'а и ранжирования.
    """

    ticker: str
    figi: str = ""
    exchange: str = ""


@dataclass(frozen=True)
class ScreenResult:
    """Вердикт допуска для одного инструмента."""

    eligible: bool
    reason: ScreenReason | None = None
    detail: str = ""


@dataclass(frozen=True)
class ScreenItem:
    """Вход Screener'а: кандидат плюс факты о его доступности.

    features_valid — вердикт Feature-слоя (ATR посчитан и close > 0).
    Сам ATR здесь не вычисляется и не хранится.
    """

    ref: InstrumentRef
    source_bars: int
    resampled_bars: int = 0
    features_valid: bool = True
    tradeable: bool = True
    blacklisted: bool = False

    @classmethod
    def for_ref(cls, ref: InstrumentRef, *, bars: int) -> "ScreenItem":
        return cls(ref=ref, source_bars=bars)


@dataclass(frozen=True)
class ScreenedInstrument:
    """Прошедший screening инструмент вместе с метаданными доступности."""

    instrument: InstrumentRef
    result: ScreenResult
    source_bars: int = 0
    resampled_bars: int = 0


@dataclass(frozen=True)
class UniverseEntry:
    """Кандидат Universe: идентичность, статика из БД и доступность данных."""

    ref: InstrumentRef
    lot: int = 10
    name: str = ""
    avg_price: float = 0.0
    avg_turnover: float = 0.0
    sector: str = ""
    bars: int = 0


@dataclass(frozen=True)
class UniverseSnapshot:
    """Детерминированный снимок кандидатов на момент as_of."""

    as_of: datetime
    source: UniverseSource
    entries: tuple[UniverseEntry, ...] = ()

    @property
    def instruments(self) -> tuple[InstrumentRef, ...]:
        return tuple(e.ref for e in self.entries)

    def with_bars(self, bars_by_figi: dict[str, int]) -> "UniverseSnapshot":
        """Возвращает копию snapshot'а с подставленной доступностью по figi."""
        return UniverseSnapshot(
            as_of=self.as_of,
            source=self.source,
            entries=tuple(
                UniverseEntry(
                    ref=e.ref,
                    lot=e.lot,
                    name=e.name,
                    avg_price=e.avg_price,
                    avg_turnover=e.avg_turnover,
                    sector=e.sector,
                    bars=bars_by_figi.get(e.ref.figi, 0),
                )
                for e in self.entries
            ),
        )
