import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.engine.research_equity_recorder import ReplayEquityRecorder


@pytest.mark.asyncio
async def test_same_timestamp_prices_form_one_portfolio_snapshot():
    start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    positions = [SimpleNamespace(figi='A', side='BUY', qty=10, entry_price=100.0),
                 SimpleNamespace(figi='B', side='SELL', qty=10, entry_price=200.0)]
    broker = SimpleNamespace(positions=AsyncMock(return_value=positions), cash=AsyncMock(return_value=10000.0))
    output = io.StringIO()
    recorder = ReplayEquityRecorder(broker=broker, output=output, timeframe=timedelta(minutes=5),
                                     max_mark_age=timedelta(minutes=5), accrued_costs=lambda at: 0.0)
    consumer = AsyncMock()
    for figi, price in [('A', 110.0), ('B', 190.0)]:
        await recorder.process(SimpleNamespace(figi=figi, ts=start, close=price), consumer)
    assert recorder.points == []
    await recorder.flush()
    assert recorder.points[0].as_of == start + timedelta(minutes=5)
    assert recorder.points[0].equity == 10200.0
    assert len(output.getvalue().splitlines()) == 1
    await recorder.flush()
    assert len(recorder.points) == 1


@pytest.mark.asyncio
async def test_next_bar_is_not_used_to_value_previous_timestamp():
    start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    broker = SimpleNamespace(positions=AsyncMock(return_value=[
        SimpleNamespace(figi='A', side='BUY', qty=10, entry_price=100.0)]), cash=AsyncMock(return_value=10000.0))
    recorder = ReplayEquityRecorder(broker=broker, output=io.StringIO(), timeframe=timedelta(minutes=5),
                                     max_mark_age=timedelta(minutes=5), accrued_costs=lambda at: None)
    async def consume(candle):
        if candle.ts > start:
            assert recorder.points[0].as_of == start + timedelta(minutes=5)
            assert recorder.marks['A'].price == 110.0
    await recorder.process(SimpleNamespace(figi='A', ts=start, close=110.0), consume)
    await recorder.process(SimpleNamespace(figi='A', ts=start + timedelta(minutes=5), close=150.0), consume)
    assert recorder.points[0].equity is None
    assert recorder.points[0].unknown == ('accrued_costs',)
