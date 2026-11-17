from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.bot.universe import compat

@pytest.mark.asyncio
async def test_volatile_selection_uses_bounded_hub_tail(monkeypatch):
    ref = SimpleNamespace(figi='OFFLINE', ticker='TEST')
    monkeypatch.setattr(compat, 'discover_liquid_universe', lambda _: SimpleNamespace(entries=[SimpleNamespace(ref=ref)]))
    preparation = AsyncMock()
    monkeypatch.setattr('app.services.candle_cache.ensure_candles', preparation)
    bars = [SimpleNamespace(high=102.0, low=98.0, close=100.0, volume=1000) for _ in range(34)]
    reader = AsyncMock(return_value=bars)
    monkeypatch.setattr(compat, 'load_bars', reader)
    legacy = AsyncMock(side_effect=AssertionError('Unbounded minute aggregation is not daily history'))
    monkeypatch.setattr(compat, 'load_all_bars', legacy)
    session = object()
    result = await compat.select_volatile_universe(session, {'TEST':'OFFLINE'}, top_n=5)
    reader.assert_awaited_once_with(session, 'OFFLINE', interval=5, limit=34)
    legacy.assert_not_awaited()
    assert len(result) == 1
    assert result[0]['ticker'] == 'TEST'
    assert result[0]['atr_pct'] == 4.0
    assert result[0]['avg_turnover'] == 100000
