"""
Tests for StreamManager — unit tests (mock gRPC, no real API calls).
"""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.bot.stream_manager import ServerOrder, ServerPosition, ServerTrade, StreamManager


@pytest.fixture
def sm():
    return StreamManager(
        token="test-token",
        account_id="test-account",
        target="sandbox-invest-public-api.tbank.ru",
    )


class TestServerPosition:
    def test_basic(self):
        sp = ServerPosition(
            figi="BBG004730N88",
            ticker="SBER",
            side="LONG",
            qty=10,
            entry_price=280.0,
        )
        assert sp.figi == "BBG004730N88"
        assert sp.side == "LONG"
        assert sp.qty == 10
        assert sp.blocked == 0


class TestServerTrade:
    def test_basic(self):
        st = ServerTrade(
            order_id="order-1",
            figi="BBG004730N88",
            direction="BUY",
            price=280.5,
            quantity=7,
            trade_id="trade-1",
            executed_at=datetime.now(timezone.utc),
        )
        assert st.order_id == "order-1"
        assert st.direction == "BUY"
        assert st.price == 280.5


class TestServerOrder:
    def test_basic(self):
        so = ServerOrder(
            order_id="order-1",
            figi="BBG004730N88",
            direction="BUY",
            status="FILLED",
        )
        assert so.status == "FILLED"


class TestStreamManagerInit:
    def test_initial_state(self, sm):
        assert sm.running is False
        assert sm.get_position("BBG004730N88") is None
        assert sm.get_positions() == []
        assert sm.get_order("order-1") is None
        assert sm.get_trade("trade-1") is None


class TestGetPosition:
    def test_returns_none_for_unknown(self, sm):
        assert sm.get_position("UNKNOWN") is None

    def test_returns_position(self, sm):
        sp = ServerPosition(
            figi="BBG004730N88", ticker="SBER", side="LONG", qty=10, entry_price=280.0
        )
        sm._positions["BBG004730N88"] = sp
        assert sm.get_position("BBG004730N88") is sp

    def test_get_positions_all(self, sm):
        sp1 = ServerPosition(figi="F1", ticker="T1", side="LONG", qty=5, entry_price=100.0)
        sp2 = ServerPosition(figi="F2", ticker="T2", side="SHORT", qty=3, entry_price=200.0)
        sm._positions["F1"] = sp1
        sm._positions["F2"] = sp2
        positions = sm.get_positions()
        assert len(positions) == 2
        assert sp1 in positions
        assert sp2 in positions


class TestWaitOrder:
    @pytest.mark.asyncio
    async def test_wait_returns_when_filled(self, sm):
        async def _fill():
            await asyncio.sleep(0.1)
            sm._orders["order-1"] = ServerOrder(
                order_id="order-1", figi="F1", direction="BUY", status="FILLED"
            )

        task = asyncio.create_task(_fill())
        result = await sm.wait_order("order-1", timeout=5.0)
        assert result is not None
        assert result.status == "FILLED"
        await task

    @pytest.mark.asyncio
    async def test_wait_timeout(self, sm):
        result = await sm.wait_order("nonexistent", timeout=0.3)
        assert result is None


class TestMapOrderStatus:
    def test_filled(self):
        assert StreamManager._map_order_status(3) == "FILLED"

    def test_new(self):
        assert StreamManager._map_order_status(1) == "NEW"

    def test_cancelled(self):
        assert StreamManager._map_order_status(4) == "CANCELLED"

    def test_rejected(self):
        assert StreamManager._map_order_status(5) == "REJECTED"

    def test_unknown(self):
        assert StreamManager._map_order_status(99) == "UNKNOWN_99"


class TestQ:
    def test_none(self):
        assert StreamManager._q(None) == 0.0

    def test_int(self):
        assert StreamManager._q(42) == 42.0

    def test_float(self):
        assert StreamManager._q(3.14) == 3.14

    def test_quotation(self):
        q = MagicMock()
        q.units = 280
        q.nano = 500000000
        assert StreamManager._q(q) == 280.5


class TestStopStart:
    @pytest.mark.asyncio
    async def test_stop_without_start(self, sm):
        await sm.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_double_start(self, sm):
        sm._running = True
        await sm.start()  # should not create duplicate tasks
        assert len(sm._tasks) == 0
        sm._running = False
