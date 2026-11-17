from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.bot.live_broker import LiveBroker


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
async def test_operations_use_bound_broker_account_and_service(mode):
    live = Mock(return_value='live-result')
    sandbox = Mock(return_value='sandbox-result')
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = mode
    broker._account = 'OFFLINE_ACCOUNT'
    broker._get_services = Mock(return_value=SimpleNamespace(
        operations=SimpleNamespace(get_operations=live),
        sandbox=SimpleNamespace(get_sandbox_operations=sandbox)))
    end = datetime(2026, 11, 16, tzinfo=timezone.utc)
    start = end-timedelta(days=3)
    assert await broker.operations(start, end) == f'{mode}-result'
    selected, other = (live, sandbox) if mode == 'live' else (sandbox, live)
    selected.assert_called_once_with(account_id='OFFLINE_ACCOUNT', from_=start, to=end)
    other.assert_not_called()


@pytest.mark.asyncio
async def test_operations_missing_account_never_calls_sdk():
    broker = LiveBroker.__new__(LiveBroker)
    broker._account = None
    broker._get_services = Mock()
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match='broker_account_missing'):
        await broker.operations(now, now)
    broker._get_services.assert_not_called()
