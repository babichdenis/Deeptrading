"""SectorMembership: принадлежность инструмента сектору/группе — ОТДЕЛЬНОЕ измерение.

Часть параллельного слоя Universe 2.0: legacy-слой НЕ менялся. Сектор — это
metadata/grouping, НЕ числовой признак MarketFeatures и НЕ фильтр Universe:
отсутствие сектора не удаляет инструмент. Потребители — Strategy Selection
и Portfolio/Allocation/Risk (например, sector exposure constraint).

API:
    sector_memberships(snapshot)      — собрать membership из UniverseSnapshot
    get_sector(memberships, inst)     — сектор инструмента или None
    group_by_sector(memberships)      — dict sector -> sorted instruments
    get_sector_members(memberships, ) — инструменты сектора (sorted) или пусто

Детерминизм: group_by_sector/get_sector_members сортируют InstrumentRef по
ticker ASC (InstrumentRef order=True), поэтому порядок не зависит от порядка
входных membership'ов.
"""
from __future__ import annotations

from collections import defaultdict

from app.bot.universe.domain import (
    InstrumentRef,
    SectorMembership,
    UniverseSnapshot,
)


def sector_memberships(snapshot: UniverseSnapshot) -> tuple[SectorMembership, ...]:
    """Собирает memberships из UniverseSnapshot (entry.sector).

    Инструменты с пустым/неизвестным сектором пропускаются — они остаются в
    Universe, просто без membership. Выход отсортирован по ticker ASC.
    """
    memberships = sorted(
        (
            SectorMembership(instrument=e.ref, sector=e.sector)
            for e in snapshot.entries
            if e.sector
        ),
        key=lambda m: m.instrument.ticker,
    )
    return tuple(memberships)


def get_sector(
    memberships: tuple[SectorMembership, ...], instrument: InstrumentRef
) -> str | None:
    """Сектор инструмента или None, если membership нет.

    Если инструмент есть с неизвестным сектором — это тоже сектор (raw value);
    пустоты к построению не попали, поэтому None означает «нет membership».
    """
    for m in memberships:
        if m.instrument.ticker == instrument.ticker:
            return m.sector
    return None


def group_by_sector(
    memberships: tuple[SectorMembership, ...]
) -> dict[str, tuple[InstrumentRef, ...]]:
    """dict sector -> инструменты, отсортированные по ticker ASC.

    Если sector присутствует в memberships, он попадёт в результат, даже если
    с пустым списком инструментов (детерминизм: известное множество секторов
    группы не меняется).
    """
    grouped: dict[str, list[InstrumentRef]] = defaultdict(list)
    for m in memberships:
        grouped[m.sector].append(m.instrument)
    return {sector: tuple(sorted(refs)) for sector, refs in grouped.items()}


def get_sector_members(
    memberships: tuple[SectorMembership, ...], sector: str
) -> tuple[InstrumentRef, ...]:
    """Инструменты сектора, отсортированные по ticker ASC. Пусто — нет таких."""
    return tuple(sorted(m.instrument for m in memberships if m.sector == sector))


def sectors_known(memberships: tuple[SectorMembership, ...]) -> tuple[str, ...]:
    """Множество известных секторов, отсортированное по алфавиту."""
    return tuple(sorted({m.sector for m in memberships}))