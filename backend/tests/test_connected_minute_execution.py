from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot.connected_execution import execute_minute
from app.engine.models import Candle, Side


AT = datetime(2026, 11, 9, 10, 32, tzinfo=timezone.utc)


class Book:
    def __init__(self):
        self.positions = {'SBER': SimpleNamespace(figi='SBER', stop=294.821908552058, target=297.936182895884, entry_fill=295.5)}
        self.closed = []
        self.events = []

    def close_position(self, position, price, at, reason):
        self.closed.append((position.figi, price, at, reason))
        del self.positions[position.figi]

    def record(self, at, kind, **fields):
        self.events.append((at, kind, fields))


def bar(open=295.07, high=295.07, low=294.66, close=294.9):
    return Candle(ts=AT, open=open, high=high, low=low, close=close, volume=100)


def test_sber_stop_checked_off_five_minute_boundary():
    book = Book()
    execute_minute(book, {'SBER': bar()}, AT, lambda: None)
    assert book.closed[0][1] == 294.821908552058
    assert book.closed[0][2].minute == 33
    assert book.closed[0][3] == 'stop_loss'


def test_open_sell_precedes_later_intraminute_stop():
    book = Book()
    execute_minute(book, {'SBER': bar()}, AT, lambda: book.close_position(book.positions['SBER'], 295.07, AT, 'ensemble_sell'))
    assert len(book.closed) == 1
    assert book.closed[0][1:] == (295.07, AT, 'ensemble_sell')


def test_gap_stop_precedes_open_action():
    book = Book()
    called = []
    execute_minute(book, {'SBER': bar(open=294.0, low=293.8)}, AT, lambda: called.append(bool(book.positions)))
    assert called == [False]
    assert book.closed[0][1:3] == (294.0, AT)


def test_stop_first_when_both_levels_touched():
    book = Book()
    execute_minute(book, {'SBER': bar(high=300)}, AT, lambda: None)
    assert book.closed[0][3] == 'stop_loss'


def test_missing_ticker_does_not_block_other_open_actions():
    book = Book()
    called = []
    execute_minute(book, {}, AT, lambda: called.append(True))
    assert called == [True]
    assert book.positions['SBER'].path_uncertainty
    assert not book.closed


def test_missing_ticker_does_not_block_another_position_stop():
    book = Book()
    book.positions['OTHER'] = SimpleNamespace(
        figi='OTHER', stop=294.821908552058, target=297.936182895884,
        entry_fill=295.5,
    )
    execute_minute(book, {'OTHER': bar()}, AT, lambda: None)
    assert list(book.positions) == ['SBER']
    assert book.positions['SBER'].path_uncertainty
    assert book.closed == [('OTHER', 294.821908552058, AT.replace(minute=33), 'stop_loss')]


def test_new_position_is_protected_in_entry_minute():
    book = Book()
    position = book.positions.pop('SBER')

    def enter():
        book.positions['SBER'] = position

    execute_minute(book, {'SBER': bar()}, AT, enter)
    assert not book.positions
    assert book.closed == [('SBER', 294.821908552058, AT.replace(minute=33), 'stop_loss')]


@pytest.mark.parametrize('offset', range(5))
@pytest.mark.parametrize('missing_other', [False, True])
def test_every_minute_protects_position_independently_of_other_tickers(offset, missing_other):
    book = Book()
    at = AT.replace(minute=30) + timedelta(minutes=offset)
    if missing_other:
        book.positions['OTHER'] = SimpleNamespace(
            figi='OTHER', stop=100, target=110, entry_fill=105,
        )
    actions = []
    execute_minute(book, {'SBER': replace(bar(), ts=at)}, at, lambda: actions.append(at))
    assert actions == [at]
    assert book.closed == [('SBER', 294.821908552058, at + timedelta(minutes=1), 'stop_loss')]
    assert list(book.positions) == (['OTHER'] if missing_other else [])
    if missing_other:
        assert book.positions['OTHER'].path_uncertainty


@pytest.mark.parametrize('offset', [-1, 1])
def test_new_entry_rejects_wrong_minute_before_callback(offset):
    book = Book()
    position = book.positions.pop('SBER')
    calls = []

    def enter():
        calls.append(True)
        book.positions['SBER'] = position

    wrong = replace(bar(), ts=AT + timedelta(minutes=offset))
    with pytest.raises(ValueError, match='timestamp mismatch'):
        execute_minute(book, {'SBER': wrong}, AT, enter)
    assert not calls
    assert not book.positions
    assert not book.closed
    assert not book.events


def test_wrong_minute_rejected_before_partial_portfolio_close():
    book = Book()
    book.positions['OTHER'] = SimpleNamespace(figi='OTHER', stop=294.8, target=300, entry_fill=295.5)
    wrong = replace(bar(), ts=AT - timedelta(minutes=1))
    with pytest.raises(ValueError, match='timestamp mismatch'):
        execute_minute(book, {'SBER': bar(open=294), 'OTHER': wrong}, AT, lambda: None)
    assert set(book.positions) == {'SBER', 'OTHER'}
    assert not book.closed
    assert not book.events


def test_missing_minute_then_gap_uses_next_real_open_and_retains_uncertainty():
    book = Book()
    execute_minute(book, {}, AT, lambda: None)
    assert not book.closed
    next_at = AT + timedelta(minutes=1)
    next_bar = replace(bar(open=293, high=294, low=292, close=293.5), ts=next_at)
    execute_minute(book, {'SBER': next_bar}, next_at, lambda: None)
    assert book.closed == [('SBER', 293, next_at, 'stop_loss')]
    assert book.path_uncertain_figis == {'SBER'}


@pytest.mark.parametrize(
    ("opening", "high", "low", "price", "minute"),
    [(301, 302, 289, 301, 32), (296, 301, 289, 300, 33)],
)
def test_short_stop_first_with_gap_priority(opening, high, low, price, minute):
    book = Book()
    pos = book.positions["SBER"]
    pos.side = Side.SELL
    pos.stop = 300
    pos.target = 290
    execute_minute(book, {"SBER": bar(open=opening, high=high, low=low)}, AT, lambda: None)
    assert book.closed == [("SBER", price, AT.replace(minute=minute), "stop_loss")]
