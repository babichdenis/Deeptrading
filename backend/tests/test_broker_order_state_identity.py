from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from t_tech.invest.schemas import OrderDirection, OrderIdType

from app.bot.live_broker import LiveBroker


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
@pytest.mark.parametrize('fault', [None, 'request', 'figi', 'side', 'requested', 'executed', 'missing_exchange', 'timeout'])
async def test_order_state_uses_request_identity_and_bound_scope(mode, fault):
    response = SimpleNamespace(order_request_id='request-id', order_id='exchange-id',
        figi='FIGI', direction=OrderDirection.ORDER_DIRECTION_BUY,
        lots_requested=3, lots_executed=1)
    changes = {'request': ('order_request_id', 'other'), 'figi': ('figi', 'OTHER'),
        'side': ('direction', OrderDirection.ORDER_DIRECTION_SELL),
        'requested': ('lots_requested', 4), 'executed': ('lots_executed', 4),
        'missing_exchange': ('order_id', '')}
    if fault in changes: setattr(response, *changes[fault])
    call = Mock(return_value=response, side_effect=TimeoutError('offline') if fault == 'timeout' else None)
    other = Mock()
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = mode
    broker._account = 'OFFLINE_ACCOUNT'
    broker._get_services = Mock(return_value=SimpleNamespace(
        orders=SimpleNamespace(get_order_state=call if mode == 'live' else other),
        sandbox=SimpleNamespace(get_sandbox_order_state=call if mode == 'sandbox' else other)))
    if fault:
        with pytest.raises(TimeoutError if fault == 'timeout' else ValueError):
            await broker.order_state('request-id', 'FIGI', 'BUY', 3)
    else:
        assert await broker.order_state('request-id', 'FIGI', 'BUY', 3) is response
    call.assert_called_once_with(account_id='OFFLINE_ACCOUNT', order_id='request-id',
                                 order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)
    other.assert_not_called()
