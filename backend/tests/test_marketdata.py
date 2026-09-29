"""Тесты marketdata: smoke, duplicate, gap, forming, close, hot-add."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.marketdata import (
    Candle,
    CandleHub,
    CandleState,
    CandleStore,
    EventBus,
    FeedStatus,
    IndicatorHub,
    Subscription,
    Symbol,
)

T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)


def _make_candle(ts: datetime, close: float = 100.0, figi: str = "BBG004730N88", tf: str = "1m") -> Candle:
    return Candle(
        ts=ts,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1000.0,
        figi=figi,
        timeframe=tf,
        source="test",
    )


class TestCandleStore:
    def test_append_and_get(self):
        store = CandleStore()
        c1 = _make_candle(T0)
        c2 = _make_candle(T0 + timedelta(minutes=1))
        store.append(c1)
        store.append(c2)
        assert len(store) == 2
        assert store.last() == c2
        assert store.get(1) == [c2]

    def test_forming_and_close(self):
        store = CandleStore()
        forming = _make_candle(T0)
        store.update_forming(forming)
        assert store.forming() == forming
        assert store.last() is None

        closed = store.close(forming)
        assert closed.state == CandleState.CLOSED
        assert store.last() == closed
        assert store.forming() is None

    def test_maxlen(self):
        store = CandleStore(maxlen=3)
        for i in range(5):
            store.append(_make_candle(T0 + timedelta(minutes=i)))
        assert len(store) == 3
        assert store.get() == [
            _make_candle(T0 + timedelta(minutes=2)),
            _make_candle(T0 + timedelta(minutes=3)),
            _make_candle(T0 + timedelta(minutes=4)),
        ]


class TestIndicatorHub:
    def test_rsi_indicator(self):
        hub = IndicatorHub()
        from app.marketdata.models import IndicatorKey
        key = IndicatorKey(figi="SBER", timeframe="5m", indicator="rsi", params=(14,))
        ind = hub.get(key)
        assert ind.state["count"] == 0

        for i in range(20):
            c = _make_candle(T0 + timedelta(minutes=i), close=100.0 + i)
            val = ind.commit(c)
        assert val is not None
        assert 0 <= val <= 100

    def test_ema_indicator(self):
        hub = IndicatorHub()
        from app.marketdata.models import IndicatorKey
        key = IndicatorKey(figi="SBER", timeframe="5m", indicator="ema", params=(20,))
        ind = hub.get(key)
        for i in range(30):
            ind.commit(_make_candle(T0 + timedelta(minutes=i), close=100.0 + i))
        assert ind.state["ema"] is not None

    def test_cache_same_key(self):
        hub = IndicatorHub()
        from app.marketdata.models import IndicatorKey
        key = IndicatorKey(figi="SBER", timeframe="5m", indicator="rsi", params=(14,))
        ind1 = hub.get(key)
        ind2 = hub.get(key)
        assert ind1 is ind2


class TestCandleHub:
    def test_add_and_remove_symbol(self):
        hub = CandleHub()
        import asyncio
        asyncio.run(hub.add_symbol("BBG004730N88", "SBER"))
        assert "BBG004730N88" in hub._symbols
        asyncio.run(hub.remove_symbol("BBG004730N88"))
        assert "BBG004730N88" not in hub._symbols

    def test_subscribe_and_store(self):
        hub = CandleHub()
        import asyncio
        asyncio.run(hub.subscribe("BBG004730N88", "1m"))
        store = hub.get_store("BBG004730N88", "1m")
        assert store is not None
        state = hub.get_subscription("BBG004730N88", "1m")
        assert state is not None

    def test_duplicate_candle(self):
        hub = CandleHub()
        import asyncio
        asyncio.run(hub.subscribe("BBG004730N88", "1m"))
        store = hub.get_store("BBG004730N88", "1m")
        state = hub.get_subscription("BBG004730N88", "1m")

        c = _make_candle(T0)
        hub._process_candle(c, state)
        assert store.forming() is not None
        seq_after_first = store.forming().sequence

        # Duplicate (same ts) — sequence should not increment
        hub._process_candle(c, state)
        assert store.forming().sequence == seq_after_first

    def test_gap_detection(self):
        hub = CandleHub()
        import asyncio
        asyncio.run(hub.subscribe("BBG004730N88", "1m"))
        store = hub.get_store("BBG004730N88", "1m")
        state = hub.get_subscription("BBG004730N88", "1m")

        c1 = _make_candle(T0)
        hub._process_candle(c1, state)

        # Gap: skip 1 minute
        c2 = _make_candle(T0 + timedelta(minutes=2))
        hub._process_candle(c2, state)
        # Gap detected (logged as warning)

    def test_event_subscription(self):
        hub = CandleHub()
        events = []
        hub.on(CandleState.FORMING, lambda e: events.append(e))
        import asyncio
        asyncio.run(hub.subscribe("BBG004730N88", "1m"))
        state = hub.get_subscription("BBG004730N88", "1m")
        hub._process_candle(_make_candle(T0), state)
        assert len(events) == 1
        assert events[0].state == "FORMING"

    def test_stats(self):
        hub = CandleHub()
        import asyncio
        asyncio.run(hub.add_symbol("BBG004730N88", "SBER"))
        asyncio.run(hub.subscribe("BBG004730N88", "1m"))
        stats = hub.stats
        assert stats["symbols"] == 1
        assert "BBG004730N88:1m" in stats["subscriptions"]


class TestEventBus:
    def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []
        bus.on_candle("FORMING", lambda e: received.append(e))
        from app.marketdata.models import CandleEvent
        event = CandleEvent(
            event_id="test", symbol="SBER", figi="BBG004730N88",
            timeframe="1m", ts=T0, candle=_make_candle(T0),
            state="FORMING", source="test", sequence=1,
        )
        bus.emit_candle(event)
        assert len(received) == 1
        assert received[0].event_id == "test"
