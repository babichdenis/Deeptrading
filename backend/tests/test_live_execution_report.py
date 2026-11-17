from types import SimpleNamespace

import pytest

from app.bot.live_broker import LiveBroker


def _broker():
    b = LiveBroker.__new__(LiveBroker)
    b._account = 'OFFLINE_ACCOUNT'
    b._log = lambda *args: None
    return b


@pytest.mark.asyncio
async def test_parser_records_full_execution_report():
    b = _broker()
    submitted = []

    def post(**kw):
        submitted.append(kw)
        return SimpleNamespace(
            executed_order_price=SimpleNamespace(units=103, nano=500_000_000),
            lots_requested=5, lots_executed=5)

    b._api_post_order = post
    ex = b._execution_from_response(post(quantity=5), 'REQUEST', 5)
    assert ex.fill_price == 103.5
    assert ex.status == 'FILLED'
    assert ex.requested_lots == 5
    assert ex.executed_lots == 5
    assert ex.remaining_lots == 0
    assert ex.fill_price == 103.5
    assert submitted[0]['quantity'] == 5


@pytest.mark.asyncio
async def test_parser_records_partial_execution_report():
    b = _broker()

    def post(**kw):
        return SimpleNamespace(
            executed_order_price=SimpleNamespace(units=103, nano=500_000_000),
            lots_requested=5, lots_executed=2)

    b._api_post_order = post
    ex = b._execution_from_response(post(quantity=5), 'REQUEST', 5)
    assert ex.fill_price == 103.5
    assert ex.status == 'PARTIAL'
    assert ex.requested_lots == 5
    assert ex.executed_lots == 2
    assert ex.remaining_lots == 3


@pytest.mark.asyncio
async def test_parser_zero_execution_is_unknown():
    b = _broker()

    def post(**kw):
        return SimpleNamespace(
            executed_order_price=SimpleNamespace(units=0, nano=0),
            lots_requested=5, lots_executed=0)

    b._api_post_order = post
    ex = b._execution_from_response(post(quantity=5), 'REQUEST', 5)
    assert ex.fill_price is None
    assert ex.status == 'UNKNOWN'
    assert ex.executed_lots == 0
    assert ex.remaining_lots == 5


def test_execution_rejects_overexecuted_quantity():
    b = _broker()
    resp = SimpleNamespace(
        executed_order_price=SimpleNamespace(units=100, nano=0),
        lots_requested=3, lots_executed=7)
    ex = b._execution_from_response(resp, 'oid', 3)
    assert ex.requested_lots == 3
    assert ex.executed_lots is None
    assert ex.remaining_lots is None
    assert ex.status == 'UNKNOWN'
    assert ex.fill_price is None


def test_execution_rejects_nonfinite_price():
    b = _broker()
    resp = SimpleNamespace(
        executed_order_price=SimpleNamespace(units=0, nano=0),
        lots_requested=3, lots_executed=3)
    ex = b._execution_from_response(resp, 'oid', 3)
    assert ex.fill_price is None
    assert ex.status == 'UNKNOWN'


def test_execution_explicit_zero_fee_differs_from_unknown():
    b = _broker()
    resp_zero = SimpleNamespace(
        executed_order_price=SimpleNamespace(units=100, nano=0),
        lots_requested=2, lots_executed=2,
        executed_commission=SimpleNamespace(units=0, nano=0))
    ex_zero = b._execution_from_response(resp_zero, 'oid', 2)
    assert ex_zero.commission == 0.0
    resp_absent = SimpleNamespace(
        executed_order_price=SimpleNamespace(units=100, nano=0),
        lots_requested=2, lots_executed=2)
    ex_absent = b._execution_from_response(resp_absent, 'oid', 2)
    assert ex_absent.commission is None


@pytest.mark.asyncio
async def test_open_reuses_client_order_id_when_provided(monkeypatch):
    from tests.test_live_entry_submission_scope import broker, request, response
    from unittest.mock import AsyncMock, Mock
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.reserve', AsyncMock(return_value=True))
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe', AsyncMock(return_value=True))
    b = broker()
    payload = request()
    payload['client_order_id'] = 'ORDER_42'
    report = response()
    report.order_request_id = 'ORDER_42'
    b._api_post_order = Mock(return_value=report)
    receipt = await b.open_position(**payload)
    assert b._api_post_order.call_args.kwargs['order_id'] == 'ORDER_42'
    assert receipt.client_order_id == 'ORDER_42'


@pytest.mark.parametrize('reported,executed', [(4, 3), (0, 0), (3, -1), (3, 1.5), (3, True), (3, None), (None, 3)])
def test_execution_requires_consistent_exact_lots(reported, executed):
    resp = SimpleNamespace(lots_requested=reported, lots_executed=executed,
                           executed_order_price=SimpleNamespace(units=100, nano=0))
    result = _broker()._execution_from_response(resp, 'oid', 3)
    assert result.status == 'UNKNOWN'
    assert result.executed_lots is None
    assert result.remaining_lots is None
    assert result.fill_price is None
