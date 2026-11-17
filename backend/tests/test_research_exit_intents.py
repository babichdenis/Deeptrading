from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot.connected_execution import execute_minute
from app.bot.research_exit_intents import ResearchExitIntents
from app.engine.models import Candle

AT = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


class Book:
    def __init__(self):
        self.positions = {'A': SimpleNamespace(figi='A', stop=95, target=110, entry_fill=100)}
        self.closed = []

    def close_position(self, position, price, at, reason):
        self.closed.append((price, at, reason))
        del self.positions[position.figi]

    def record(self, *args, **kwargs):
        pass


@pytest.mark.parametrize('opening,reason', [(98, 'ENSEMBLE_SELL'), (90, 'stop_loss')])
def test_wait_then_next_open_with_stop_priority(opening, reason):
    book = Book()
    queue = ResearchExitIntents()
    queue.request(book.positions['A'], AT)
    queue.request(book.positions['A'], AT + timedelta(seconds=1))
    execute_minute(book, {}, AT, lambda: queue.execute(book, {}, AT))
    assert not book.closed
    at = AT + timedelta(minutes=1)
    bars = {'A': Candle(ts=at, open=opening, high=opening+1, low=opening-1, close=opening, volume=10)}
    execute_minute(book, bars, at, lambda: queue.execute(book, bars, at))
    queue.execute(book, bars, at)
    assert book.closed == [(opening, at, reason)]
    assert not queue.pending
    assert book.path_uncertain_figis == {'A'}


def test_old_intent_cannot_close_replacement_position():
    book = Book()
    queue = ResearchExitIntents()
    queue.request(book.positions['A'], AT)
    book.positions['A'] = SimpleNamespace(figi='A')
    queue.execute(book, {'A': SimpleNamespace(ts=AT, open=98)}, AT)
    assert not book.closed
    assert not queue.pending
    assert 'A' in book.positions


def test_end_without_reference_preserves_intent_and_position():
    book = Book()
    queue = ResearchExitIntents()
    queue.request(book.positions['A'], AT)
    queue.execute(book, {}, AT + timedelta(minutes=5))
    assert queue.pending['A'].position is book.positions['A']
    assert not book.closed
