from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from t_tech.invest.schemas import OrderDirection

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
@pytest.mark.parametrize('case,status,executed,expected', [
    ('cancelled', 3, 0, 'CANCELLED'), ('rejected', 2, 0, 'REJECTED'),
    ('new', 4, 0, 'PENDING_RECONCILIATION'), ('partial', 5, 1, 'PENDING_RECONCILIATION'),
    ('filled', 1, 3, 'PENDING_RECONCILIATION'), ('cancelled_partial', 3, 1, 'PENDING_RECONCILIATION'),
    ('timeout', 3, 0, 'PENDING_RECONCILIATION'), ('regressed', 3, 0, 'PARTIAL_FILLED'),
    ('identity', 3, 0, 'PENDING_RECONCILIATION')])
async def test_pending_reconciliation_requires_terminal_zero_execution(mode, case, status, executed, expected):
    response = SimpleNamespace(order_request_id='other' if case == 'identity' else 'request',
        order_id='exchange', figi='FIGI', direction=OrderDirection.ORDER_DIRECTION_BUY,
        lots_requested=3, lots_executed=executed, execution_report_status=status)
    get_state = Mock(return_value=response, side_effect=TimeoutError() if case == 'timeout' else None)
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = mode
    broker._account = 'OFFLINE_ACCOUNT'
    broker._get_services = lambda: SimpleNamespace(
        orders=SimpleNamespace(get_order_state=get_state),
        sandbox=SimpleNamespace(get_sandbox_order_state=get_state))
    order = SimpleNamespace(id='request', action='open', side='BUY', qty=2,
        status='PARTIAL_FILLED' if case == 'regressed' else 'PENDING_RECONCILIATION',
        meta={'requested_lots': 3, 'lot_size': 10})
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker = broker
    rt.pending_orders = {'FIGI': order}
    rt.events = SimpleNamespace(log=Mock())
    await rt._reconcile_pending_orders()
    assert order.status == expected
    assert ('FIGI' not in rt.pending_orders) == (expected in ('CANCELLED', 'REJECTED'))
    assert get_state.call_args.kwargs['account_id'] == 'OFFLINE_ACCOUNT'
    assert get_state.call_args.kwargs['order_id'] == 'request'
