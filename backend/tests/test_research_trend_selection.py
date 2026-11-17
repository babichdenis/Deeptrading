from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.domain import InstrumentRef, MarketFeatures, TrendFeatures, VolatilityFeatures
from app.bot.universe.research_trend_selection import select_volatile_trends


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def feature(ticker, atr, strength):
    ref = InstrumentRef(ticker, ticker)
    return MarketFeatures(ref, NOW,
        VolatilityFeatures(ref, NOW, atr_pct=atr, valid=True),
        TrendFeatures(ref, NOW, strength=strength, valid=True))


def select(items, **kwargs):
    return select_volatile_trends(items, as_of=NOW,
        eligible=frozenset(f.instrument for f in items), min_strength=0.1, top_n=2, **kwargs)


def test_volatile_chop_rejected_and_eligible_trends_ranked_by_volatility():
    items = [feature('CHOP', 20, 0.01), feature('STRONG', 1, 0.9), feature('FAST', 3, 0.2)]
    result = select(items)
    assert [r.instrument.ticker for r in result.selected] == ['FAST', 'STRONG']
    assert result.rejected == ((items[0].instrument, 'weak_trend'),)
    assert result == select(items[::-1])


def test_tie_break_and_zero_limit():
    items = [feature('B', 2, 0.5), feature('A', 2, 0.5)]
    assert [r.instrument.ticker for r in select(items).selected] == ['A', 'B']
    assert not select_volatile_trends(items, as_of=NOW, eligible=frozenset(), min_strength=0.1, top_n=0).selected


@pytest.mark.parametrize('part', ['as_of', 'volatility', 'trend'])
def test_other_snapshot_rejected(part):
    item = feature('A', 2, 0.5)
    future = NOW + timedelta(minutes=1)
    item = replace(item, **{part: future if part == 'as_of' else replace(getattr(item, part), as_of=future)})
    result = select([item])
    assert not result.selected
    assert result.rejected[0][1] == 'snapshot_mismatch'


@pytest.mark.parametrize('atr,strength,reason', [(float('nan'), 0.5, 'invalid_volatility'), (2, float('inf'), 'invalid_strength'), (0, 0.5, 'invalid_volatility')])
def test_invalid_numeric_features_rejected(atr, strength, reason):
    result = select([feature('A', atr, strength)])
    assert not result.selected
    assert result.rejected[0][1] == reason


def test_duplicate_figi_rejected():
    item = feature('A', 2, 0.5)
    with pytest.raises(ValueError, match='duplicate'):
        select([item, item])


def test_tradability_is_required():
    item = feature('A', 2, 0.5)
    result = select_volatile_trends([item], as_of=NOW, eligible=frozenset(), min_strength=0.1, top_n=2)
    assert result.rejected[0][1] == 'ineligible'
    assert not result.selected
