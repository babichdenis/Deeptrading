from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot.gates import MarketContext, gate_orderbook


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def context(book, imbalance=0.3, spread=25.0):
    if book is not None:
        book = {"ts": NOW.isoformat(), **book}
    return MarketContext(
        cfg=SimpleNamespace(entry_ob_imbalance_max=imbalance, entry_ob_spread_max=spread),
        side="BUY", orderbook=book, orderbook_as_of=NOW,
    )


@pytest.mark.parametrize('state', ['empty', 'one_sided', 'crossed', 'inconsistent', 'unknown'])
def test_invalid_state_cannot_admit_entry(state):
    result = gate_orderbook(context({'state': state, 'imbalance': 0, 'spread_bps': 1}))
    assert not result.passed
    assert result.key == 'orderbook_error'


@pytest.mark.parametrize('field,value', [
    ('imbalance', None), ('imbalance', float('nan')), ('imbalance', 2),
    ('spread_bps', None), ('spread_bps', float('inf')), ('spread_bps', -1),
])
def test_invalid_enabled_measurement_cannot_admit_entry(field, value):
    book = {'state': 'ok', 'imbalance': 0, 'spread_bps': 1, field: value}
    assert not gate_orderbook(context(book)).passed


def test_actual_empty_api_shape_is_rejected():
    book = {'state': 'empty', 'is_consistent': True, 'imbalance': None, 'spread_bps': None}
    assert not gate_orderbook(context(book)).passed


def test_consistency_failure_is_rejected_even_with_good_metrics():
    book = {'state': 'ok', 'is_consistent': False, 'imbalance': 0, 'spread_bps': 1}
    assert not gate_orderbook(context(book)).passed


def test_valid_book_and_explicit_disabled_policy():
    assert gate_orderbook(context({'state': 'ok', 'imbalance': 0, 'spread_bps': 1})).passed
    assert gate_orderbook(context(None, imbalance=0, spread=0)).passed
    assert gate_orderbook(context({'state': 'ok', 'spread_bps': 1}, imbalance=0)).passed


@pytest.mark.parametrize('ts', [None, '', 'bad', '2026-11-15T12:00:00',
                                 (NOW - timedelta(seconds=30, microseconds=1)).isoformat(),
                                 (NOW + timedelta(microseconds=1)).isoformat()])
def test_unknown_stale_and_future_time_rejected(ts):
    result = gate_orderbook(context({'state': 'ok', 'ts': ts, 'imbalance': 0, 'spread_bps': 1}))
    assert not result.passed
    assert result.key == 'orderbook_error'


def test_freshness_boundary_and_timezone_equivalence():
    source = NOW - timedelta(seconds=30)
    for stamp in (source.isoformat(), source.astimezone(timezone(timedelta(hours=3))).isoformat()):
        assert gate_orderbook(context({'state': 'ok', 'ts': stamp, 'imbalance': 0, 'spread_bps': 1})).passed


def test_missing_time_and_state_rejected_without_fixture_defaults():
    cfg = SimpleNamespace(entry_ob_imbalance_max=0.3, entry_ob_spread_max=25)
    for book in ({'state': 'ok', 'imbalance': 0, 'spread_bps': 1},
                 {'ts': NOW.isoformat(), 'imbalance': 0, 'spread_bps': 1}):
        assert not gate_orderbook(MarketContext(cfg=cfg, side='BUY', orderbook=book, orderbook_as_of=NOW)).passed
