import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.runtime_telemetry import RuntimeTelemetryMixin


@pytest.mark.asyncio
@pytest.mark.parametrize('feed, expected', [('replay', 'bull'), ('live', 'neutral')])
async def test_market_regime_replay_uses_current_bars(monkeypatch, feed, expected):
    monkeypatch.setitem(sys.modules, 'app.api.routes.screener', SimpleNamespace(market_breadth=lambda: {}))
    now = datetime(2026, 9, 1, 7, 50, tzinfo=timezone.utc)
    context = SimpleNamespace(
        config=SimpleNamespace(feed=feed),
        _regime_cache=(time.monotonic(), {'state': 'neutral'}),
        _imoex_buf=[(now - timedelta(minutes=60), 100.0),
                    (now - timedelta(minutes=20), 100.3), (now, 100.4)],
        _bot_now=lambda: now,
    )
    result = await RuntimeTelemetryMixin.market_regime(context)
    assert result['state'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('feed, expected_equity', [('replay', 10500.0), ('live', 20000.0)])
async def test_portfolio_replay_reads_current_account(feed, expected_equity):
    broker = SimpleNamespace(equity=AsyncMock(return_value=10500.0), positions=AsyncMock(return_value=[
        SimpleNamespace(figi='test', ticker='TEST', side='BUY', qty=10, entry_price=100.0)
    ]))
    context = SimpleNamespace(
        config=SimpleNamespace(feed=feed), broker=broker,
        _pf_cache=(time.monotonic(), {'equity': 20000.0}),
        tcs_to_bbg={}, buffers={'test': [SimpleNamespace(close=110.0)]}, tickers={},
        sector_meta=AsyncMock(return_value={'TEST': {'sector': 'financial', 'beta': 1.0}}),
        get_no_trade_stats=lambda: {}, trade_history=AsyncMock(return_value={}),
        mtf_macd_map=AsyncMock(return_value={}), daily_bias_map=AsyncMock(return_value={}),
        market_regime=AsyncMock(return_value={'state': 'neutral'}),
        _cand_queue={}, _hist_cache={}, _equity_peak=10500.0, _dd_level_done=0,
    )
    result = await RuntimeTelemetryMixin.portfolio_snapshot(context)
    assert result['equity'] == expected_equity
    if feed == 'replay':
        broker.equity.assert_awaited_once()
        broker.positions.assert_awaited_once()
        assert result['gross_notional'] == 1000.0
    else:
        broker.equity.assert_not_awaited()
