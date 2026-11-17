from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.runtime_telemetry import RuntimeTelemetryMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["equity", "positions"])
async def test_unknown_broker_portfolio_does_not_become_empty_snapshot(failed_method):
    runtime = RuntimeTelemetryMixin()
    runtime.config = SimpleNamespace(feed="replay")
    runtime._pf_cache = None
    runtime.broker = SimpleNamespace(
        equity=AsyncMock(return_value=20000.0),
        positions=AsyncMock(return_value=[]),
    )
    failure = TimeoutError("offline portfolio unavailable")
    getattr(runtime.broker, failed_method).side_effect = failure
    with pytest.raises(TimeoutError) as caught:
        await runtime.portfolio_snapshot()
    assert caught.value is failure
    assert runtime._pf_cache is None


@pytest.mark.asyncio
async def test_entry_refresh_does_not_reuse_recent_display_snapshot():
    import time

    runtime = RuntimeTelemetryMixin()
    runtime.config = SimpleNamespace(feed="live")
    cached = {"equity": 20000.0, "positions": []}
    runtime._pf_cache = (time.monotonic(), cached)
    runtime.broker = SimpleNamespace(equity=AsyncMock(side_effect=TimeoutError("offline")))
    assert await runtime.portfolio_snapshot() is cached
    runtime.broker.equity.assert_not_awaited()
    with pytest.raises(TimeoutError):
        await runtime.portfolio_snapshot(ttl=0.0)
    runtime.broker.equity.assert_awaited_once()
