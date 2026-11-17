from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from t_tech.invest.schemas import MoneyValue, OrderDirection, OrderExecutionReportStatus, OrderStage, OrderState

from app.services.broker_fill_evidence import parse_order_state_fills, read_order_fill_evidence


NOW = datetime(2026,11,16,12,tzinfo=timezone.utc)


def response():
    return OrderState(order_id='EXCHANGE',order_request_id='REQUEST',figi='FIGI',
                      direction=OrderDirection.ORDER_DIRECTION_BUY,lots_requested=3,lots_executed=3,
                      execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
                      currency='rub',order_date=NOW-timedelta(minutes=3),executed_order_price=MoneyValue(currency='rub',units=99999,nano=0),
                      stages=[OrderStage(price=MoneyValue(currency='rub',units=100,nano=0),quantity=1,
                                         trade_id='FIRST',execution_time=NOW-timedelta(minutes=2)),
                              OrderStage(price=MoneyValue(currency='rub',units=103,nano=0),quantity=2,
                                         trade_id='SECOND',execution_time=NOW-timedelta(minutes=1))])


def arguments():
    return dict(mode='live',broker_account_id='OFFLINE_ACCOUNT',client_order_id='REQUEST',
                figi='FIGI',side='BUY',requested_lots=3,lot_size=10,as_of=NOW)


def test_sdk_stages_supply_weighted_price_units_and_actual_times():
    value = parse_order_state_fills(response(),**arguments())
    assert value.units == 30 and value.notional == Decimal('3060') and value.average_price == Decimal('102')
    assert [fill.units for fill in value.fills] == [10,20]
    assert value.first_execution_time == NOW-timedelta(minutes=2)
    assert value.last_execution_time == NOW-timedelta(minutes=1)
    assert value.average_price != Decimal('99999')


@pytest.mark.parametrize('case', ['request','figi','direction','requested','missing_stage','duplicate',
                                  'future','naive','currency','zero_price','quantity','missing_time','before_order','default_time','numeric_direction'])
def test_inconsistent_broker_evidence_is_not_reconstructed(case):
    value = response()
    if case == 'request': value.order_request_id='OTHER'
    if case == 'figi': value.figi='OTHER'
    if case == 'direction': value.direction=OrderDirection.ORDER_DIRECTION_SELL
    if case == 'requested': value.lots_requested=4
    if case == 'missing_stage': value.stages.pop()
    if case == 'duplicate': value.stages[1].trade_id=value.stages[0].trade_id
    if case == 'future': value.stages[0].execution_time=NOW+timedelta(seconds=1)
    if case == 'naive': value.stages[0].execution_time=NOW.replace(tzinfo=None)
    if case == 'currency': value.stages[0].price.currency='usd'
    if case == 'zero_price': value.stages[0].price.units=0
    if case == 'quantity': value.stages[0].quantity=True
    if case == 'missing_time': value.stages[0].execution_time=None
    if case == 'before_order': value.stages[0].execution_time=NOW-timedelta(minutes=4)
    if case == 'default_time': value.stages[0].execution_time=datetime(1970,1,1,tzinfo=timezone.utc)
    if case == 'numeric_direction': value.direction=1.0
    with pytest.raises(ValueError): parse_order_state_fills(value,**arguments())


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['live','sandbox'])
async def test_history_lookup_preserves_scope_and_original_request(mode):
    broker = SimpleNamespace(mode=mode,_account='OFFLINE_ACCOUNT',order_state=AsyncMock(return_value=response()))
    args = {**arguments(),'mode':mode}
    value = await read_order_fill_evidence(broker,**args)
    assert value.mode == mode and value.broker_account_id == 'OFFLINE_ACCOUNT'
    broker.order_state.assert_awaited_once_with('REQUEST','FIGI','BUY',3)


@pytest.mark.asyncio
async def test_account_change_during_history_lookup_refuses_evidence():
    broker = SimpleNamespace(mode='live',_account='OFFLINE_ACCOUNT')
    async def get_state(*args):
        broker._account='OTHER'
        return response()
    broker.order_state=AsyncMock(side_effect=get_state)
    with pytest.raises(ValueError,match='during history lookup'):
        await read_order_fill_evidence(broker,**arguments())


@pytest.mark.parametrize('status,executed,accepted', [
    (1,3,True),(1,1,False),(2,1,False),(2,3,False),
    (3,1,True),(3,3,True),(4,1,False),(4,3,False),
    (5,1,True),(5,3,False),(0,1,False),(True,1,False),
])
def test_fill_history_status_agrees_with_executed_quantity(status,executed,accepted):
    value = response()
    value.execution_report_status = status
    value.lots_executed = executed
    if executed == 1:
        value.stages = value.stages[:1]
    if accepted:
        evidence = parse_order_state_fills(value,**arguments())
        assert evidence.units == executed * 10
    else:
        with pytest.raises(ValueError,match='cumulative fill evidence'):
            parse_order_state_fills(value,**arguments())
