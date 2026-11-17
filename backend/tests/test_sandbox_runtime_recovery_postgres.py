from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, select
from t_tech.invest.schemas import MoneyValue, OrderState

from app.bot.live_broker import LiveBroker
from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_open_completion_postgres import initialized
from tests.test_close_stage_settlement_postgres import NOW


@pytest.mark.asyncio
async def test_sdk_aggregate_recovers_protection_and_releases_own_pause(pg_engine, monkeypatch):
    journal, original, rt = await initialized(pg_engine)
    async with journal.sessions() as db:
        await db.execute(delete(BrokerOrderIntent))
        await db.commit()
    original = replace(original, mode='sandbox')
    assert await journal.reserve(original)
    rt.pending_orders.clear()
    rt._entry_pause().blockers.clear()
    rt.broker.mode = rt.broker_mode = 'sandbox'
    rt.broker._get_lot = AsyncMock(return_value=10)
    raw = OrderState(order_id='EXCHANGE',order_request_id=original.client_order_id,
        figi=original.figi,direction=1,lots_requested=2,lots_executed=20,
        execution_report_status=1,currency='rub',order_date=NOW+timedelta(minutes=1),
        executed_order_price=MoneyValue(currency='rub',units=2100,nano=0),
        total_order_amount=MoneyValue(currency='rub',units=2100,nano=0),
        executed_commission=MoneyValue(currency='rub',units=1,nano=50000000),stages=[])
    rt.broker._get_services = Mock(return_value=SimpleNamespace(
        sandbox=SimpleNamespace(get_sandbox_order_state=Mock(return_value=raw))))
    rt.broker.order_state = MethodType(LiveBroker.order_state, rt.broker)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW+timedelta(hours=1)
    monkeypatch.setattr('app.bot.live_broker.datetime',Clock)
    await rt._restore_open_intents()
    await rt._reconcile_pending_orders()
    assert not rt.pending_orders and not rt.entries_paused
    assert rt._held == {'FIGI'} and rt._exit_qty['FIGI']==20
    assert rt._trail_stop['FIGI']==float(original.stop_loss)
    assert rt._exit_plans['FIGI']==original.exit_policy.restore()
    rt.broker._api_post_order.assert_not_called()
    async with journal.sessions() as db:
        positions=list(await db.scalars(select(SandboxTrade)))
    assert len(positions)==1
    import json
    proof=rt._entry_record_matches(positions[0],json.loads(positions[0].meta),
        figi='FIGI',side='LONG',units=20,entry_price=105,lot=10)
    assert proof[1]=='sandbox_order_timestamp'
    assert (await journal.load(original)).status=='SETTLED'


@pytest.mark.asyncio
async def test_four_sdk_aggregates_recover_each_protection_without_resubmission(pg_engine, monkeypatch):
    from uuid import uuid4
    journal, original, rt = await initialized(pg_engine)
    async with journal.sessions() as db:
        await db.execute(delete(BrokerOrderIntent))
        await db.commit()
    intents = [replace(original, mode='sandbox', figi=f'FIGI{i}', client_order_id=str(uuid4())) for i in range(4)]
    for intent in intents:
        assert await journal.reserve(intent)
    rt.pending_orders.clear()
    rt._entry_pause().blockers.clear()
    rt.broker.mode = rt.broker_mode = 'sandbox'
    rt.broker._get_lot = AsyncMock(return_value=10)
    responses = {intent.client_order_id: OrderState(
        order_id=f'EXCHANGE{i}', order_request_id=intent.client_order_id,
        figi=intent.figi, direction=1, lots_requested=2, lots_executed=20,
        execution_report_status=1, currency='rub', order_date=NOW+timedelta(minutes=1),
        executed_order_price=MoneyValue(currency='rub', units=2100, nano=0),
        total_order_amount=MoneyValue(currency='rub', units=2100, nano=0),
        executed_commission=MoneyValue(currency='rub', units=1, nano=50000000), stages=[])
        for i, intent in enumerate(intents)}
    rt.broker._get_services = Mock(return_value=SimpleNamespace(sandbox=SimpleNamespace(
        get_sandbox_order_state=Mock(side_effect=lambda **kwargs: responses[kwargs['order_id']]))))
    rt.broker.order_state = MethodType(LiveBroker.order_state, rt.broker)
    rt.broker.get_position = AsyncMock(side_effect=lambda figi: SimpleNamespace(figi=figi, side='LONG', qty=20, entry_price=105))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW+timedelta(hours=1)
    monkeypatch.setattr('app.bot.live_broker.datetime', Clock)
    await rt._restore_open_intents()
    await rt._reconcile_pending_orders()
    await rt._reconcile_pending_orders()
    assert not rt.pending_orders and not rt.entries_paused
    assert rt._held == {intent.figi for intent in intents}
    for intent in intents:
        assert rt._exit_qty[intent.figi] == 20
        assert rt._trail_stop[intent.figi] == float(intent.stop_loss)
        assert rt._exit_target[intent.figi] == float(intent.take_profit)
        assert rt._exit_plans[intent.figi] == intent.exit_policy.restore()
        assert (await journal.load(intent)).status == 'SETTLED'
    rt.broker._api_post_order.assert_not_called()
    async with journal.sessions() as db:
        positions = list(await db.scalars(select(SandboxTrade)))
    assert len(positions) == 4
