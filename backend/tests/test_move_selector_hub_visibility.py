from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.bot.universe.move_select import _load_candles

@pytest.mark.asyncio
@pytest.mark.parametrize('interval,seconds', [(24,86400),(4,3600)])
async def test_move_history_excludes_unclosed_and_outside_window(monkeypatch, interval, seconds):
    start = datetime(2026,11,10,tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds*3, minutes=20)
    timestamps = [start-timedelta(seconds=seconds),start,start+timedelta(seconds=seconds*2),start+timedelta(seconds=seconds*3)]
    bars = [SimpleNamespace(ts=ts,open=100,high=102,low=99,close=101) for ts in timestamps]
    reader = AsyncMock(return_value=bars)
    monkeypatch.setattr('app.bot.universe.bars._reader',lambda:SimpleNamespace(tail_bars=reader))
    result = await _load_candles(object(),['TEST','TEST'],interval,start,end)
    assert [row[0] for row in result['TEST']] == timestamps[1:3]
    reader.assert_awaited_once()
    assert reader.await_args.kwargs['as_of'] == end

@pytest.mark.asyncio
async def test_move_history_rejects_naive_as_of():
    with pytest.raises(ValueError,match='aware'):
        await _load_candles(object(),['TEST'],24,datetime(2026,11,10),datetime(2026,11,12))
