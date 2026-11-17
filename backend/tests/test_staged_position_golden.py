from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.staged_position import ConfirmedFill, StagedPositionBook


START = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def fill(n, side, shares, price, commission, intent):
    return ConfirmedFill(str(n), 'TEST', side, shares, price, commission,
                         START + timedelta(minutes=n), intent)


@pytest.mark.parametrize('side, opposite, add_price, close_price', [
    ('BUY', 'SELL', 110, 120), ('SELL', 'BUY', 90, 80),
])
def test_staged_round_trip_cash_and_fee_conservation(side, opposite, add_price, close_price):
    book = StagedPositionBook('TEST')
    book.apply(fill(1, side, 10, 100, 1, 'increase'))
    book.apply(fill(2, side, 10, add_price, 2, 'increase'))
    assert book.state.average_entry == (100 + add_price) / 2
    first = book.apply(fill(3, opposite, 5, close_price, .5, 'reduce'))
    assert first.gross_pnl == 75
    assert first.allocated_entry_commission == .75
    assert first.closed_net_pnl == 73.75
    assert book.state.shares == 15
    assert book.state.cash_delta == 71.5
    last = book.apply(fill(4, opposite, 15, close_price, 1.5, 'reduce'))
    assert last.closed_net_pnl == 221.25
    assert book.state.shares == 0
    assert book.state.side is None
    assert book.state.unallocated_entry_commission == 0
    assert book.state.realized_net == book.state.cash_delta == 295
    assert StagedPositionBook.rebuild('TEST', book.fills).state == book.state


def test_add_after_partial_exit_keeps_remaining_cost_basis():
    book = StagedPositionBook('TEST')
    book.apply(fill(1, 'BUY', 10, 100, 1, 'increase'))
    book.apply(fill(2, 'SELL', 5, 110, .5, 'reduce'))
    book.apply(fill(3, 'BUY', 5, 120, .6, 'increase'))
    assert book.state.average_entry == 110
    assert book.state.unallocated_entry_commission == pytest.approx(1.1)
    assert book.state.realized_net == 49
    book.apply(fill(4, 'SELL', 10, 115, 1, 'reduce'))
    assert book.state.realized_net == pytest.approx(96.9)
    assert book.state.cash_delta == pytest.approx(96.9)


def test_duplicate_fill_after_later_fill_and_restart_has_no_second_effect():
    book = StagedPositionBook('TEST')
    first = fill(1, 'BUY', 10, 100, 1, 'increase')
    book.apply(first)
    book.apply(fill(2, 'SELL', 5, 110, .5, 'reduce'))
    book = StagedPositionBook.rebuild('TEST', book.fills)
    before = book.state
    result = book.apply(first)
    assert not result.applied and result.cash_delta == 0
    assert book.state == before and len(book.fills) == 2
    with pytest.raises(ValueError, match='Conflicting'):
        book.apply(replace(first, shares=20))
    assert book.state == before


@pytest.mark.parametrize('invalid', [
    fill(2, 'SELL', 11, 100, 0, 'reduce'),
    fill(2, 'BUY', 1, 100, 0, 'reduce'),
    fill(2, 'SELL', 1, 100, 0, 'increase'),
    fill(0, 'BUY', 1, 100, 0, 'increase'),
    fill(2, 'BUY', 0, 100, 0, 'increase'),
    fill(2, 'BUY', 1, float('nan'), 0, 'increase'),
    fill(2, 'BUY', 1, 100, float('nan'), 'increase'),
    replace(fill(2, 'BUY', 1, 100, 0, 'increase'), at=START.replace(tzinfo=None)),
])
def test_invalid_execution_never_mutates_book(invalid):
    book = StagedPositionBook('TEST')
    book.apply(fill(1, 'BUY', 10, 100, 1, 'increase'))
    before = book.state
    with pytest.raises(ValueError):
        book.apply(invalid)
    assert book.state == before and len(book.fills) == 1


def test_partial_broker_fills_count_confirmed_shares_only():
    book = StagedPositionBook('TEST')
    book.apply(fill(1, 'BUY', 3, 100, .15, 'increase'))
    assert book.state.shares == 3
    book.apply(fill(2, 'BUY', 7, 101, .35, 'increase'))
    assert book.state.shares == 10
    assert book.state.average_entry == 100.7
    assert book.state.cash_delta == -.5
