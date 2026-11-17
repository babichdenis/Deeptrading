from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.bot.live_broker import LiveBroker, LivePosition


@pytest.mark.asyncio
@pytest.mark.parametrize('lot_size', [1, 10])
@pytest.mark.parametrize('executed_lots', [0, 1, 2])
async def test_close_records_only_confirmed_executed_units(lot_size, executed_lots):
    stored = []
    submitted = []
    broker = LiveBroker.__new__(LiveBroker)
    broker._account = 'OFFLINE_ACCOUNT'
    broker._log = lambda *args: None
    broker.config = SimpleNamespace(commission_rate=0.003)
    position = LivePosition('OFFLINE_FIGI', 'TEST', 'LONG', 2 * lot_size, 100,
                            datetime(2026, 11, 15, tzinfo=timezone.utc), None, None)

    async def get_position(figi):
        return position

    async def get_lot(figi):
        return lot_size

    def post(**request):
        submitted.append(request)
        return SimpleNamespace(
            executed_order_price=SimpleNamespace(units=101, nano=0),
            lots_requested=2, lots_executed=executed_lots,
            execution_report_status=1 if executed_lots == 2 else (5 if executed_lots else 4),
        )

    class MemorySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalar(self, query):
            return SimpleNamespace(id=1)

        def add(self, trade):
            stored.append(trade)

        async def commit(self):
            pass

    broker.get_position = get_position
    broker._get_lot = get_lot
    broker._api_post_order = post
    broker.sessions = MemorySession
    await broker.close_position('OFFLINE_FIGI', 101, 'offline')
    assert len(submitted) == 1
    assert submitted[0]['quantity'] == 2
    recorded_units = sum(trade.qty for trade in stored)
    assert recorded_units == executed_lots * lot_size
    if executed_lots == 0:
        assert stored == []


@pytest.mark.asyncio
@pytest.mark.parametrize('position_units', [0, 5, 15, -10, 10.5, True])
async def test_close_never_rounds_unknown_or_non_lot_position_into_an_order(position_units):
    broker = LiveBroker.__new__(LiveBroker)
    broker._account = 'OFFLINE_ACCOUNT'
    broker._log = lambda *args: None
    position = LivePosition('OFFLINE_FIGI', 'TEST', 'LONG', position_units, 100,
                            datetime(2026, 11, 15, tzinfo=timezone.utc), None, None)
    async def get_position(figi):
        return position
    async def get_lot(figi):
        return 10
    submitted = []
    def post(**request):
        submitted.append(request)
        return SimpleNamespace(lots_executed=0,
                               executed_order_price=SimpleNamespace(units=0, nano=0))
    broker.get_position = get_position
    broker._get_lot = get_lot
    broker._api_post_order = post
    await broker.close_position('OFFLINE_FIGI', 101, 'offline')
    assert submitted == []
