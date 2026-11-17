from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.strategy_history import update_strategy_history


def bar(minute, close=100):
    return SimpleNamespace(ts=datetime(2026, 11, 16, tzinfo=timezone.utc) + timedelta(minutes=minute), close=close)


def test_preload_future_is_not_visible_to_current_decision():
    buffer = deque([bar(0), bar(5), bar(10)], maxlen=100)
    update_strategy_history(buffer, bar(5, 101))
    assert [b.ts.minute for b in buffer] == [0, 5]
    assert buffer[-1].close == 101


def test_repeated_timestamp_replaces_without_duplicate():
    buffer = deque([bar(0), bar(5)], maxlen=100)
    update_strategy_history(buffer, bar(5, 102))
    assert len(buffer) == 2
    assert buffer[-1].close == 102


def test_history_stays_ordered_and_bounded():
    buffer = deque([bar(5), bar(0), bar(5)], maxlen=3)
    update_strategy_history(buffer, bar(10))
    update_strategy_history(buffer, bar(15))
    assert [b.ts.minute for b in buffer] == [5, 10, 15]
