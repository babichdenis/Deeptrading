from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle
from app.engine.wave1 import RsiReversalStrategy


def bars():
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)
    prices = [100 - i * 0.5 for i in range(30)] + [85.8, 86.0, 85.9, 87.0, 88.0]
    return [Candle(ts=start + timedelta(minutes=5 * i), open=p, high=p + .1,
                   low=p - .1, close=p, volume=100) for i, p in enumerate(prices)]


def state(strategy):
    return (strategy._avg_gain, strategy._avg_loss, strategy._prev_rsi,
            strategy._prev_close, strategy._last_processed_ts)


def test_preload_matches_continuous_stream_and_returns_only_latest_signal():
    history = bars()
    streaming = RsiReversalStrategy()
    signals = [streaming.on_bar([bar]) for bar in history]
    assert any(signal is not None for signal in signals)
    preloaded = RsiReversalStrategy()
    assert preloaded.on_bar(history) == signals[-1]
    assert state(preloaded) == state(streaming)


def test_repeated_and_sliding_history_does_not_double_count():
    history = bars()
    subject = RsiReversalStrategy()
    subject.on_bar(history[:-1])
    before = state(subject)
    assert subject.on_bar(history[:-1]) is None
    assert state(subject) == before
    subject.on_bar(history[-10:])
    reference = RsiReversalStrategy()
    reference.on_bar(history)
    assert state(subject) == state(reference)


def test_reset_rebuilds_same_state():
    subject = RsiReversalStrategy()
    subject.on_bar(bars())
    expected = state(subject)
    subject.reset()
    subject.on_bar(bars())
    assert state(subject) == expected


def test_nonmonotonic_history_rejected_before_state_change():
    subject = RsiReversalStrategy()
    before = state(subject)
    with pytest.raises(ValueError):
        subject.on_bar(list(reversed(bars())))
    assert state(subject) == before
