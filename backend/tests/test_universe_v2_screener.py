"""StrategyScreener (Universe 2.0): разделение Eligibility/Strategy.

Тесты §25 (separation block) и §17:
  30. same Universe can feed Trend screener.
  31. same Universe can feed MeanReversion screener.
  32. Trend screening does not modify Universe.
  33. MeanReversion screening does not modify Universe.
  34. Sector grouping does not modify Universe.
  35. Selection does not create orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.universe.domain import InstrumentRef, MarketFeatures, TrendDirection, VolatilityFeatures
from app.bot.universe.features import compute_market_features
from app.bot.universe.screener import MeanReversionScreener, TrendStrengthScreener, screen_by_strategy
from app.engine.models import Candle as EngineCandle

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")
LKOH = InstrumentRef(ticker="LKOH", figi="FIGI_LKOH")


def _bars(closes, v: float = 1.0):
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) + v
        lo = min(prev, c) - v
        bars.append(EngineCandle(ts=T0 + STEP * i, open=prev, high=hi, low=lo, close=c, volume=1000.0))
        prev = c
    return bars


def _market_features_many():
    sber = compute_market_features(SBER, _bars([100 + 0.2 * i for i in range(60)]), as_of=T0 + STEP * 59)
    lkoh = compute_market_features(LKOH, _bars([100.0] * 60), as_of=T0 + STEP * 59)
    return (sber, lkoh)


# ---------------------------------------------------------------- separation


def test_same_universe_feeds_both_screeners():
    sber, lkoh = _market_features_many()
    universe = [m.instrument for m in (sber, lkoh)]
    assert len(universe) == 2  # один и тот же «юниверс»

    trend = screen_by_strategy([sber, lkoh], TrendStrengthScreener(min_strength=0.05))
    mrev = screen_by_strategy([sber, lkoh], MeanReversionScreener(max_strength=0.01))

    trend_accepted = [r.instrument for r in trend if r.accepted]
    mrev_accepted = [r.instrument for r in mrev if r.accepted]
    # трендовый скринер берёт SBER (сильный тренд), mean-reversion — LKOH (плоский)
    assert SBER in trend_accepted
    assert LKOH not in trend_accepted
    assert LKOH in mrev_accepted
    assert SBER not in mrev_accepted


def test_trend_screening_does_not_modify_universe():
    sber, lkoh = _market_features_many()
    before = [sber.instrument, lkoh.instrument]
    screen_by_strategy([sber, lkoh], TrendStrengthScreener(min_strength=0.05))
    after = [sber.instrument, lkoh.instrument]
    assert before == after  # MarketFeatures — immutable, скринер не мутирует


def test_meanreversion_screening_does_not_modify_universe():
    sber, lkoh = _market_features_many()
    before = [sber, lkoh]
    screen_by_strategy([sber, lkoh], MeanReversionScreener(max_strength=0.01))
    assert [sber, lkoh] == before


def test_sector_grouping_does_not_modify_universe():
    from app.bot.universe.domain import UniverseEntry, UniverseSnapshot, UniverseSource
    from app.bot.universe.sectors import group_by_sector, sector_memberships

    snapshot = UniverseSnapshot(
        as_of=T0,
        source=UniverseSource.ELIGIBLE_TABLE,
        entries=(UniverseEntry(ref=SBER, lot=10, sector="Finance", bars=100),),
    )
    entries_before = snapshot.entries
    memberships = sector_memberships(snapshot)
    group_by_sector(memberships)
    assert snapshot.entries == entries_before


def test_screen_score_is_available():
    sber, _ = _market_features_many()
    results = screen_by_strategy([sber], TrendStrengthScreener(min_strength=0.0))
    assert results[0].accepted
    assert results[0].score > 0.0
    assert results[0].instrument == SBER


def test_screen_rejects_invalid_trend():
    from app.bot.universe.domain import TrendFeatures

    mf = MarketFeatures(
        instrument=SBER,
        as_of=T0,
        volatility=VolatilityFeatures(SBER, T0, valid=False),
        trend=TrendFeatures(SBER, T0, valid=False, reason="no_data"),
    )
    res = screen_by_strategy([mf], TrendStrengthScreener(min_strength=0.05))
    assert not res[0].accepted


def test_direction_up_down_is_measurement_not_signal():
    """direction=UP/DOWN не создаёт ордеров и не решает о входе."""
    sber, lkoh = _market_features_many()
    # В Фичерах нет score/weight там, где теоретически мог бы быть «сигнал»
    assert not hasattr(sber, "buy")
    assert not hasattr(sber.trend, "entry")
    assert sber.trend.direction is TrendDirection.UP  # это измерение
    assert lkoh.trend.direction is TrendDirection.FLAT