from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.live_broker import EntrySubmissionUnknown, LiveBroker


@pytest.fixture(autouse=True)
def durable_storage(monkeypatch):
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.reserve', AsyncMock(return_value=True))
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe', AsyncMock(return_value=True))


def broker():
    value = LiveBroker.__new__(LiveBroker)
    value.mode = 'live'
    value._account = 'OFFLINE'
    value.last_execution = SimpleNamespace(fill_price=999)
    value._log = Mock()
    value.sessions = object()
    value._get_lot = AsyncMock(return_value=10)
    value.flush_portfolio = AsyncMock()
    value.get_position = AsyncMock(return_value=None)
    return value


def request():
    from app.services.exit_policy_snapshot import ExitPolicySnapshot
    from app.engine.exits import FixedSlTpPolicy
    return dict(figi='FIGI', ticker='TEST', side='BUY', qty=2, price=100,
                stop_loss=95, take_profit=110, strategy_id='OFFLINE', client_order_id='REQUEST',
                plan_meta=dict(lot_size=10, exit_policy=ExitPolicySnapshot.capture(FixedSlTpPolicy()).to_payload()))


def response():
    return SimpleNamespace(order_id='EXCHANGE', order_request_id='REQUEST', figi='FIGI', direction=1,
        execution_report_status=1, lots_requested=2, lots_executed=2,
        executed_order_price=SimpleNamespace(units=100, nano=0))


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ConnectionError('lost response'), TimeoutError('timeout'), RuntimeError('transport')])
async def test_any_unconfirmed_submission_preserves_unknown_outcome(error):
    value = broker()
    value._api_post_order = Mock(side_effect=error)
    with pytest.raises(EntrySubmissionUnknown) as caught:
        await value.open_position(**request())
    assert type(caught.value.__cause__) is type(error)
    assert caught.value.__cause__.args == error.args
    assert value.last_execution is None
    value._api_post_order.assert_called_once()
    assert value._api_post_order.call_args.kwargs['expected_scope'] == ('live', 'OFFLINE')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['account', 'mode'])
async def test_scope_change_after_send_cannot_publish_fill(change):
    value = broker()
    def post(**kwargs):
        assert kwargs['account_id'] == 'OFFLINE'
        if change == 'account': value._account = 'SECOND'
        else: value.mode = 'sandbox'
        return response()
    value._api_post_order = Mock(side_effect=post)
    with pytest.raises(EntrySubmissionUnknown):
        await value.open_position(**request())
    assert value.last_execution is None


@pytest.mark.asyncio
async def test_service_lookup_cannot_redirect_entry():
    value = broker()
    services = SimpleNamespace(orders=SimpleNamespace(post_order=Mock()),
                               sandbox=SimpleNamespace(post_sandbox_order=Mock()))
    def lookup():
        value.mode = 'sandbox'
        return services
    value._get_services = lookup
    with pytest.raises(EntrySubmissionUnknown):
        await value.open_position(**request())
    services.orders.post_order.assert_not_called()
    services.sandbox.post_sandbox_order.assert_not_called()


@pytest.mark.asyncio
async def test_stable_scope_returns_actual_fill_and_original_request():
    value = broker()
    value._api_post_order = Mock(return_value=response())
    receipt = await value.open_position(**request())
    assert receipt.client_order_id == 'REQUEST' and receipt.observed_units == 20
    assert value.last_execution is None
    assert value._api_post_order.call_args.kwargs['account_id'] == 'OFFLINE'


@pytest.mark.asyncio
async def test_response_processing_failure_remains_unknown_after_post(monkeypatch):
    value = broker()
    value._api_post_order = Mock(return_value=response())
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe',
                        AsyncMock(side_effect=ValueError('observation failure')))
    with pytest.raises(EntrySubmissionUnknown):
        await value.open_position(**request())
    value._api_post_order.assert_called_once()
    assert value.last_execution is None
