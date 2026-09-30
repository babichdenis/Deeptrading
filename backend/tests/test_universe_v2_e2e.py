"""E2E (Universe 2.0): новый архитектурный конвейер Universe → Allocation.

Главный тест §26: один и тот же Universe (SBER/LKOH/GMKN) питает разные
стратегии; Universe не фильтруется по trend/volatility и остаётся одинаковым,
а Trend-стратегия и MeanReversion-стратегия берут разные подмножества.

Далее §25 требования: Selection не создаёт ордеров, Allocation продолжает
работать без изменения семантики.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.universe.allocation import EqualWeightAllocation
from app.bot.universe.domain import (
    InstrumentRef,
    SelectionItem,
    SelectionResult,
    SectorMembership,
    UniverseEntry,
    UniverseSnapshot,
    UniverseSource,
)
from app.bot.universe.features import compute_market_features
from app.bot.universe.screener import MeanReversionScreener, TrendStrengthScreener
from app.bot.universe.sectors import sector_memberships, sectors_known
from app.engine.models import Candle as EngineCandle

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")
LKOH = InstrumentRef(ticker="LKOH", figi="FIGI_LKOH")
GMKN = InstrumentRef(ticker="GMKN", figi="FIGI_GMKN")


def _bars(closes, v: float = 1.0):
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) + v
        lo = min(prev, c) - v
        bars.append(EngineCandle(ts=T0 + STEP * i, open=prev, high=hi, low=lo, close=c, volume=1000.0))
        prev = c
    return bars


def _market_features():
    as_of = T0 + STEP * 59
    sber = compute_market_features(SBER, _bars([100 + 0.2 * i for i in range(60)]), as_of=as_of)
    lkoh = compute_market_features(LKOH, _bars([100.0] * 60), as_of=as_of)
    gmkn = compute_market_features(GMKN, _bars([100 - 0.2 * i for i in range(60)]), as_of=as_of)
    return sber, lkoh, gmkn


def _universe_snapshot() -> UniverseSnapshot:
    return UniverseSnapshot(
        as_of=T0 + STEP * 59,
        source=UniverseSource.ELIGIBLE_TABLE,
        entries=(
            UniverseEntry(ref=SBER, lot=10, sector="Finance", bars=60),
            UniverseEntry(ref=LKOH, lot=10, sector="Oil", bars=60),
            UniverseEntry(ref=GMKN, lot=10, sector="Metals", bars=60),
        ),
    )


def _selection_result(markets, screener, as_of) -> SelectionResult:
    """Selection поверх StrategyScreener: принимает только accepted (план §18)."""
    accepted = [m for m in markets if screener.accepts(m).accepted]
    ranked = sorted(((screener.accepts(m).score, m.instrument) for m in accepted))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].ticker))
    items = tuple(
        SelectionItem(instrument=ref, score=s, rank=i + 1)
        for i, (s, ref) in enumerate(ranked)
    )
    return SelectionResult(
        as_of=as_of, items=items, selected=items, ranking_method=screener.name, top_n=len(items)
    )


# ---------------------------------------------------------------- core E2E


def test_e2e_same_universe_feeds_trend_and_meanreversion():
    sber, lkoh, gmkn = _market_features()
    universe = _universe_snapshot()
    as_of = universe.as_of

    # 1. Universe одинаковый и НЕ фильтруется по тренду/волатильности
    instruments = universe.instruments
    assert instruments == (SBER, LKOH, GMKN)

    # 2. Sector — отдельное измерение, не фильтр
    memberships = sector_memberships(universe)
    assert sectors_known(memberships) == ("Finance", "Metals", "Oil")

    # 3. MarketFeatures для всех трёх
    markets = [sber, lkoh, gmkn]
    assert all(m.valid for m in markets)

    # 4. Trend strategy: сильный тренд (SBER up, GMKN down) — LKOH (flat) мимо
    trend_screener = TrendStrengthScreener(min_strength=0.05)
    trend_sel = _selection_result(markets, trend_screener, as_of)
    trend_selected = {i.instrument for i in trend_sel.selected}
    assert LKOH not in trend_selected
    assert SBER in trend_selected  # strong uptrend
    assert GMKN in trend_selected  # strong downtrend (направление не важно — важно сила)

    # 5. Mean-reversion strategy: плоский LKOH — а тренды мимо
    mrev_screener = MeanReversionScreener(max_strength=0.01)
    mrev_sel = _selection_result(markets, mrev_screener, as_of)
    mrev_selected = {i.instrument for i in mrev_sel.selected}
    assert LKOH in mrev_selected
    assert SBER not in mrev_selected
    assert GMKN not in mrev_selected

    # 6. Selection не создаёт ордеров: у SelectionItem нет side/quantity/order
    for item in trend_sel.items:
        assert not hasattr(item, "side")
        assert not hasattr(item, "quantity")


def test_e2e_universe_does_not_change_across_strategies():
    sber, lkoh, gmkn = _market_features()
    universe = _universe_snapshot()
    as_of = universe.as_of
    markets = [sber, lkoh, gmkn]

    before = universe

    _selection_result(markets, TrendStrengthScreener(min_strength=0.05), as_of)
    _selection_result(markets, MeanReversionScreener(max_strength=0.01), as_of)
    _selection_result(markets, TrendStrengthScreener(min_strength=0.0), as_of)

    assert universe == before  # скрининг/селекция не мутирует Universe


def test_e2e_allocates_to_target_portfolio():
    sber, lkoh, gmkn = _market_features()
    universe = _universe_snapshot()
    as_of = universe.as_of
    markets = [sber, lkoh, gmkn]

    trend_sel = _selection_result(markets, TrendStrengthScreener(min_strength=0.05), as_of)
    # Allocation на существующем EqualWeightAllocation — новая архитектура
    # подаёт данные в прежний Selection (план §22: allocation не переделываем)
    prices = {m.instrument.ticker: 100.0 for m in trend_sel.selected}
    lots = {m.instrument.ticker: 10 for m in trend_sel.selected}
    portfolio = EqualWeightAllocation().allocate(
        selection=trend_sel, equity=100_000.0, prices=prices, lot_sizes=lots, as_of=as_of
    )
    # TargetPortfolio: равные веса 1/N, без ордеров
    n = len(trend_sel.selected)
    assert len(portfolio.positions) == n
    assert all(round(p.target_weight, 6) == round(1.0 / n, 6) for p in portfolio.positions)
    assert portfolio.validate() == ()


# ---------------------------------------------------------------- docs-constraint test


def test_sector_membership_is_metadata_not_numeric():
    """Sector — это группа, а не числовой признак MarketFeatures."""
    memberships = sector_memberships(_universe_snapshot())
    assert all(isinstance(m, SectorMembership) for m in memberships)
    sber, _, _ = _market_features()
    assert sber.volatility is not None and sber.trend is not None
    assert not hasattr(sber, "sector")  # sector не часть MarketFeatures