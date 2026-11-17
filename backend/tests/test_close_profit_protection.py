from types import SimpleNamespace

from app.bot.close_profit_protection import CloseProfitProtection
from app.bot.connected_execution import execute_minute
from tests.test_connected_minute_execution import AT, Book, bar


def position():
    return SimpleNamespace(figi='SBER', entry_raw=100., entry_fill=100.02, entry_commission=.05001, qty=1, atr=1., stop=96., target=108.)


def test_no_activation_before_closed_price_threshold():
    p = position()
    assert CloseProfitProtection().stop_after_close(p, bar(close=101.99, high=104)) == 96.


def test_stop_never_moves_back():
    p = position()
    p.stop = 104.
    assert CloseProfitProtection().stop_after_close(p, bar(close=102.)) == 104.


def test_update_cannot_stop_position_on_same_bar():
    book = Book()
    book.positions['SBER'] = position()
    execute_minute(book, {'SBER': bar(open=100., high=103., low=99., close=102.)}, AT, lambda: None, CloseProfitProtection())
    assert not book.closed
    assert book.positions['SBER'].stop == 101.
    assert book.events[-1][1] == 'STOP_UPDATED'


def test_old_stop_executes_before_close_update():
    book = Book()
    book.positions['SBER'] = position()
    execute_minute(book, {'SBER': bar(open=100., high=103., low=95., close=102.)}, AT, lambda: None, CloseProfitProtection())
    assert book.closed[0][1] == 96.
    assert not book.events


def test_updated_stop_applies_to_next_minute_gap():
    from dataclasses import replace
    from datetime import timedelta
    book = Book()
    book.positions['SBER'] = position()
    policy = CloseProfitProtection()
    execute_minute(book, {'SBER': bar(open=100., high=103., low=99., close=102.)}, AT, lambda: None, policy)
    next_at = AT + timedelta(minutes=1)
    next_bar = replace(bar(open=100.5, high=101., low=100., close=100.8), ts=next_at)
    execute_minute(book, {'SBER': next_bar}, next_at, lambda: None, policy)
    assert book.closed[0][1:3] == (100.5, next_at)
