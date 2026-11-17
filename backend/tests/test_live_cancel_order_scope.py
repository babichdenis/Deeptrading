from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from t_tech.invest.schemas import OrderIdType

from app.bot.live_broker import LiveBroker


def broker(mode):
    value = LiveBroker.__new__(LiveBroker)
    value.mode, value._account = mode, 'ORIGINAL'
    services = SimpleNamespace(orders=SimpleNamespace(cancel_order=Mock(return_value=object())),
                               sandbox=SimpleNamespace(cancel_sandbox_order=Mock(return_value=object())))
    value._get_services = Mock(return_value=services)
    value._flush_portfolio = Mock()
    return value, services


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
async def test_cancel_uses_original_request_id_and_exact_service(mode):
    value, services = broker(mode)
    assert await value.request_cancel_order('REQUEST', expected_scope=(mode, 'ORIGINAL')) is None
    called = services.orders.cancel_order if mode == 'live' else services.sandbox.cancel_sandbox_order
    other = services.sandbox.cancel_sandbox_order if mode == 'live' else services.orders.cancel_order
    called.assert_called_once_with(account_id='ORIGINAL', order_id='REQUEST', order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)
    other.assert_not_called()
    value._flush_portfolio.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before', 'lookup', 'after'])
@pytest.mark.parametrize('field,new_value', [('_account', 'OTHER'), ('mode', 'sandbox')])
async def test_cancel_cannot_move_to_another_account(phase, field, new_value):
    value, services = broker('live')
    if phase == 'before': setattr(value, field, new_value)
    elif phase == 'lookup':
        def changed():
            setattr(value, field, new_value)
            return services
        value._get_services.side_effect = changed
    else:
        def sent(**kwargs): setattr(value, field, new_value)
        services.orders.cancel_order.side_effect = sent
    with pytest.raises(ValueError, match='cancel_scope_changed'):
        await value.request_cancel_order('REQUEST', expected_scope=('live', 'ORIGINAL'))
    assert services.orders.cancel_order.call_count == (1 if phase == 'after' else 0)
    services.sandbox.cancel_sandbox_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_timeout_is_not_terminal_confirmation():
    value, services = broker('live')
    services.orders.cancel_order.side_effect = TimeoutError('unknown cancellation')
    with pytest.raises(TimeoutError):
        await value.request_cancel_order('REQUEST', expected_scope=('live', 'ORIGINAL'))
    value._flush_portfolio.assert_called_once()
