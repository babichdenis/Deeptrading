from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timezone

import pytest
from t_tech.invest.schemas import OrderDirection

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
@pytest.mark.parametrize('status,executed,terminal,prior_units', [(3, 0, True, 0), (4, 0, False, 0), (3, 1, False, 0), (3, 0, False, 10)])
async def test_pending_close_is_observed_without_erasing_exposure(mode, status, executed, terminal, prior_units, monkeypatch):
    response = SimpleNamespace(order_request_id='close-request', order_id='exchange-close',
        figi='FIGI', direction=OrderDirection.ORDER_DIRECTION_SELL,
        lots_requested=2, lots_executed=executed, execution_report_status=status)
    get_state = Mock(return_value=response)
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = mode
    broker._account = 'OFFLINE_ACCOUNT'
    broker.sessions = object()
    stored = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe', stored)
    broker._get_services = lambda: SimpleNamespace(
        orders=SimpleNamespace(get_order_state=get_state),
        sandbox=SimpleNamespace(get_sandbox_order_state=get_state))
    order = SimpleNamespace(id='close-request', action='close', side='SELL', qty=20,
        created_at=datetime(2026,11,16,tzinfo=timezone.utc),
        status='PENDING_RECONCILIATION', meta={'requested_lots': 2, 'lot_size': 10,
        'requested_units': 20, 'observed_executed_units': prior_units})
    runtime = PaperBotRuntime.__new__(PaperBotRuntime)
    runtime.broker = broker
    runtime.pending_orders = {'FIGI': order}
    runtime.events = SimpleNamespace(log=Mock())
    runtime._held = {'FIGI'}
    runtime._exit_plans = {'FIGI': object()}
    runtime._exit_qty = {'FIGI': 20}
    await runtime._reconcile_pending_orders()
    if executed * 10 < prior_units:
        stored.assert_not_awaited()
    else:
        stored.assert_awaited_once()
    get_state.assert_called_once()
    assert get_state.call_args.kwargs['order_id'] == 'close-request'
    assert get_state.call_args.kwargs['account_id'] == 'OFFLINE_ACCOUNT'
    assert 'FIGI' in runtime._held and 'FIGI' in runtime._exit_plans
    if terminal:
        assert order.status == 'CANCELLED'
        assert 'FIGI' not in runtime.pending_orders
    else:
        assert runtime.pending_orders['FIGI'] is order
        assert order.status != 'FILLED'
