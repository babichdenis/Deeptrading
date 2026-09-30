"""SectorMembership (Universe 2.0): сектор как отдельное измерение.

Тесты §25 (sector block) и §6-§7:
  9. instrument -> sector.
  10. group_by_sector.
  11. unknown sector.
  12. instrument без sector остаётся в Universe.
  13. sector membership deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.bot.universe.domain import InstrumentRef, UniverseEntry, UniverseSnapshot, UniverseSource
from app.bot.universe.sectors import (
    get_sector,
    get_sector_members,
    group_by_sector,
    sector_memberships,
    sectors_known,
)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")
LKOH = InstrumentRef(ticker="LKOH", figi="FIGI_LKOH")
GMKN = InstrumentRef(ticker="GMKN", figi="FIGI_GMKN")


def _snapshot(with_unknown_sector: bool = False) -> UniverseSnapshot:
    entries = [
        UniverseEntry(ref=SBER, lot=10, sector="Finance", bars=100),
        UniverseEntry(ref=LKOH, lot=10, sector="Oil", bars=100),
        UniverseEntry(ref=GMKN, lot=10, sector="Metals", bars=100),
    ]
    if with_unknown_sector:
        entries.append(UniverseEntry(ref=InstrumentRef(ticker="XXX", figi="FIGI_XXX"), lot=10, bars=50))
    return UniverseSnapshot(as_of=T0, source=UniverseSource.ELIGIBLE_TABLE, entries=tuple(entries))


# ---------------------------------------------------------------- membership


def test_instrument_to_sector():
    memberships = sector_memberships(_snapshot())
    assert get_sector(memberships, SBER) == "Finance"
    assert get_sector(memberships, LKOH) == "Oil"


def test_group_by_sector():
    memberships = sector_memberships(_snapshot())
    grouped = group_by_sector(memberships)
    assert grouped["Finance"] == (SBER,)
    assert grouped["Metals"] == (GMKN,)
    assert set(grouped) == {"Finance", "Oil", "Metals"}


def test_get_sector_members():
    memberships = sector_memberships(_snapshot())
    assert get_sector_members(memberships, "Oil") == (LKOH,)
    assert get_sector_members(memberships, "Finance") == (SBER,)


def test_sectors_known_sorted():
    memberships = sector_memberships(_snapshot())
    assert sectors_known(memberships) == ("Finance", "Metals", "Oil")


def test_unknown_sector_returns_empty():
    memberships = sector_memberships(_snapshot())
    assert get_sector(memberships, InstrumentRef(ticker="UNKNOWN")) is None
    assert get_sector_members(memberships, "Quantum") == ()


# ---------------------------------------------------------------- no-filtering semantics


def test_instrument_without_sector_stays_in_universe():
    snapshot = _snapshot(with_unknown_sector=True)
    assert len(snapshot.entries) == 4  # XXX не удалён из Universe
    memberships = sector_memberships(snapshot)
    # membership нет, но инструмент в Universe
    assert get_sector(memberships, InstrumentRef(ticker="XXX")) is None
    assert snapshot.instruments == (
        InstrumentRef(ticker="SBER", figi="FIGI_SBER"),
        InstrumentRef(ticker="LKOH", figi="FIGI_LKOH"),
        InstrumentRef(ticker="GMKN", figi="FIGI_GMKN"),
        InstrumentRef(ticker="XXX", figi="FIGI_XXX"),
    )


def test_empty_snapshot_gives_empty_memberships():
    memberships = sector_memberships(UniverseSnapshot(as_of=T0, source=UniverseSource.ELIGIBLE_TABLE))
    assert memberships == ()
    assert group_by_sector(memberships) == {}
    assert sectors_known(memberships) == ()


# ---------------------------------------------------------------- determinism


def test_group_by_sector_deterministic_order():
    memberships = sector_memberships(_snapshot())
    g1 = group_by_sector(memberships)
    g2 = group_by_sector(memberships)
    assert g1 == g2


def test_memberships_sorted_by_ticker():
    memberships = sector_memberships(_snapshot())
    assert [m.instrument.ticker for m in memberships] == ["GMKN", "LKOH", "SBER"]