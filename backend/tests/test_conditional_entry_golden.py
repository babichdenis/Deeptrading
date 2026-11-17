from datetime import datetime, timedelta, timezone

import pytest

from app.engine.conditional_entry import ConditionalEntryBook
from app.engine.staged_position import ConfirmedFill, StagedPositionBook


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
TF = timedelta(minutes=1)


def create(book, key='a', side='BUY', threshold=.01):
    return book.create(intent_id=key, group_id='position1', figi='TEST', side=side,
        lots=1, lot_size=10, anchor_price=100, confirmation_fraction=threshold,
        created_at=T, expires_at=T + 5 * TF)


@pytest.mark.parametrize('side, close, next_open', [('BUY', 101, 103), ('SELL', 99, 97)])
def test_closed_confirmation_next_open_gap_and_no_duplicate_dispatch(side, close, next_open):
    book = ConditionalEntryBook()
    create(book, side=side)
    with pytest.raises(ValueError, match='Future'):
        book.observe_close('a', bar_start=T, timeframe=TF, close=close, as_of=T, thesis_valid=True)
    assert book.get('a').status == 'waiting'
    book.observe_close('a', bar_start=T, timeframe=TF, close=close, as_of=T + TF, thesis_valid=True)
    calls = []
    def reserve(ticket):
        calls.append(ticket)
        return True
    ticket = book.dispatch_at_open('a', at=T + TF, open_price=next_open, approve_and_reserve=reserve)
    assert ticket.reference_price == next_open
    assert ticket.shares == 10
    assert book.dispatch_at_open('a', at=T + TF, open_price=next_open, approve_and_reserve=reserve) is None
    assert len(calls) == 1


def test_expiration_and_thesis_invalidation_take_priority_over_trigger():
    for as_of, valid, expected in [(T + 5 * TF, True, 'expired'), (T + TF, False, 'cancelled')]:
        book = ConditionalEntryBook()
        create(book)
        book.observe_close('a', bar_start=T, timeframe=TF, close=102, as_of=as_of, thesis_valid=valid)
        assert book.get('a').status == expected


def test_two_triggers_share_one_atomic_reservation_budget():
    book = ConditionalEntryBook()
    budget = [1500.0]
    def reserve(ticket):
        required = ticket.reference_price * ticket.shares
        if required > budget[0]:
            return False
        budget[0] -= required
        return True
    for key in ('a', 'b'):
        create(book, key)
        book.observe_close(key, bar_start=T, timeframe=TF, close=101, as_of=T + TF, thesis_valid=True)
    assert book.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=reserve)
    assert book.dispatch_at_open('b', at=T + TF, open_price=102, approve_and_reserve=reserve) is None
    assert budget[0] == 480
    assert book.get('b').reason == 'risk_or_capital'


def test_reservation_unknown_outcome_does_not_allow_retry():
    book = ConditionalEntryBook()
    create(book)
    book.observe_close('a', bar_start=T, timeframe=TF, close=101, as_of=T + TF, thesis_valid=True)
    def fail(_):
        raise RuntimeError('reservation timeout')
    with pytest.raises(RuntimeError):
        book.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=fail)
    assert book.get('a').status == 'reconciliation'
    assert book.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=lambda _: True) is None
    assert book.cancel_group('position1') == ('a',)


def test_staged_fill_then_protective_close_cancels_untriggered_add():
    intents = ConditionalEntryBook()
    positions = StagedPositionBook('TEST')
    create(intents, 'a')
    create(intents, 'b', threshold=.03)
    intents.observe_close('a', bar_start=T, timeframe=TF, close=101, as_of=T + TF, thesis_valid=True)
    ticket = intents.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=lambda _: True)
    positions.apply(ConfirmedFill('entry1', 'TEST', 'BUY', ticket.shares, 102, .51, T + TF, 'increase'))
    positions.apply(ConfirmedFill('stop1', 'TEST', 'SELL', 10, 100, .5, T + 2 * TF, 'reduce'))
    assert positions.state.shares == 0
    assert positions.state.cash_delta == pytest.approx(-21.01)
    assert intents.cancel_group('position1') == ('a',)
    intents.observe_close('b', bar_start=T + 2 * TF, timeframe=TF, close=104,
                          as_of=T + 3 * TF, thesis_valid=True)
    assert intents.get('b').status == 'cancelled'
    assert intents.dispatch_at_open('b', at=T + 3 * TF, open_price=104,
                                    approve_and_reserve=lambda _: True) is None
    with pytest.raises(ValueError, match='closed'):
        create(intents, 'c')


@pytest.mark.parametrize('cancel', [False, True])
def test_uncertain_or_cancelled_reservation_does_not_dispatch(cancel):
    book = ConditionalEntryBook()
    create(book)
    book.observe_close('a', bar_start=T, timeframe=TF, close=101, as_of=T + TF, thesis_valid=True)
    def reserve(_):
        if cancel:
            book.cancel_group('position1')
            return True
        return None
    assert book.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=reserve) is None
    assert book.get('a').status == 'reconciliation'


def test_cannot_dispatch_using_observations_later_than_execution():
    book = ConditionalEntryBook()
    create(book)
    for minute in (0, 1):
        book.observe_close('a', bar_start=T + minute * TF, timeframe=TF,
            close=101, as_of=T + (minute + 1) * TF, thesis_valid=True)
    with pytest.raises(ValueError, match='before'):
        book.dispatch_at_open('a', at=T + TF, open_price=102, approve_and_reserve=lambda _: True)
