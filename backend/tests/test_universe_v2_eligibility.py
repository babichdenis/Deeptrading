"""Eligibility (Universe 2.0): «можно ли рассматривать инструмент как кандидата».

Тесты §25 (universe/eligibility block) и §4:
  1. Universe содержит инструмент с низкой волатильностью (не отсеивает по ней).
  2. Universe содержит range/flat instrument (не отсеивает по тренду).
  3. Universe не фильтрует по trend.
  4. Universe не фильтрует по volatility.
  5. Universe не делает Top-N.
  6. Universe сохраняет eligibility semantics (legacy parity).
  7. Universe deterministic.
  8. Universe respects as_of.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.bot.universe.domain import InstrumentRef, ScreenItem
from app.bot.universe.eligibility import eligible_instruments, eligibility_screen
from app.bot.universe.screener import ELIGIBLE

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)

REF = InstrumentRef(ticker="TEST", figi="FIGI")


def _items() -> list[ScreenItem]:
    return [
        ScreenItem(ref=REF, source_bars=10, resampled_bars=30, features_valid=True),
        ScreenItem(
            ref=InstrumentRef(ticker="NOBARS", figi="FIGI2"),
            source_bars=0,
            resampled_bars=0,
        ),
        ScreenItem(
            ref=InstrumentRef(ticker="BLACK", figi="FIGI3"),
            source_bars=10,
            blacklisted=True,
        ),
    ]


# ---------------------------------------------------------------- semantics


def test_eligible_profile_keeps_all_reasons():
    results = eligibility_screen(_items(), ELIGIBLE)
    assert (results[0].eligible, results[0].reasons) == (True, ())
    assert (results[1].eligible, results[1].reasons) == (False, ("no_data",))
    assert (results[2].eligible, results[2].reasons) == (False, ("blacklisted",))


def test_universe_does_not_filter_by_trend_or_volatility():
    # ELIGIBLE-профиль НЕ знает ни про ATR, ни про trend: инструмент с нулевой
    # волатильностью и плоским трендом проходит, пока есть данные/история.
    item = ScreenItem(ref=REF, source_bars=10, resampled_bars=30)
    assert eligibility_screen([item], ELIGIBLE)[0].eligible


def test_universe_keeps_instrument_with_low_volatility_and_flat_trend():
    """Универсальный профиль допускает и дешёвый и дорогой/плоский инструмент."""
    for (low_vol, flat) in ((True, True), (True, False), (False, True), (False, False)):
        item = ScreenItem(ref=REF, source_bars=10, resampled_bars=30)
        res = eligibility_screen([item], ELIGIBLE)[0]
        assert res.eligible


def test_eligible_instruments_order_preserved():
    got = eligible_instruments(_items(), ELIGIBLE)
    assert got == (REF,)


def test_insufficient_bars_not_eligible():
    item = ScreenItem(ref=REF, source_bars=1, resampled_bars=1)
    assert not eligibility_screen([item], ELIGIBLE)[0].eligible


# ---------------------------------------------------------------- determinism / as_of


def test_eligibility_deterministic():
    a = eligibility_screen(_items(), ELIGIBLE)
    b = eligibility_screen(_items(), ELIGIBLE)
    assert a == b


def test_no_rating_no_topn_in_universe_snapshot():
    """UniverseSnapshot в новом слое не несёт ATR-ранкинг/score/Top-N."""
    from app.bot.universe.domain import UniverseEntry, UniverseSnapshot, UniverseSource

    snapshot = UniverseSnapshot(
        as_of=T0,
        source=UniverseSource.ELIGIBLE_TABLE,
        entries=(UniverseEntry(ref=REF, lot=10, sector="X", bars=100),),
    )
    entry = snapshot.entries[0]
    # у UniverseEntry нет ни score, ни atr, ни weight, ни direction
    assert not hasattr(entry, "score")
    assert not hasattr(entry, "atr_pct")
    assert not hasattr(entry, "weight")
    assert not hasattr(entry, "trend")


def test_snapshot_respects_as_of():
    """as_of фиксирует, что UniverseSnapshot видит на момент среза."""
    from app.bot.universe.domain import UniverseEntry, UniverseSnapshot, UniverseSource

    snapshot = UniverseSnapshot(
        as_of=T0,
        source=UniverseSource.ELIGIBLE_TABLE,
        entries=(UniverseEntry(ref=REF, lot=10, sector="X", bars=100),),
    )
    assert snapshot.as_of == T0
    assert snapshot.entries[0].bars == 100