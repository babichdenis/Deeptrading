import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from t_tech.invest.schemas import MoneyValue, OrderStage, OrderState

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.paper import PaperAccount, PaperTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_close_stage_settlement_postgres import setup, observe, evidence, NOW
from tests.test_runtime_close_recovery_postgres import runtime


@pytest.mark.asyncio
async def test_deferred_close_stays_reserved_and_survives_restart_until_atomic_application(pg_engine):
    journal,original=await setup(pg_engine)
    await observe(journal,original,2,'FILLED')
    result=await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                                evidence=evidence(original),defer_runtime=True)
    assert not result.released
    assert not await journal.reserve(replace(original,client_order_id='SECOND'))
    repeated=await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=evidence(original))
    assert repeated.applied_units==0 and not repeated.released
    await pg_engine.dispose()
    rt=runtime(pg_engine)
    assert await rt._restore_close_intents()==1
    assert rt.pending_orders['FIGI'].qty==0 and rt.entries_paused
    calls=[]
    async def apply(db,saved,trades):
        assert saved==original
        calls.append([trade.id for trade in trades])
        await db.execute(update(PaperAccount).where(PaperAccount.id==1).values(
            cash=PaperAccount.cash+sum(trade.net_pnl for trade in trades)))
    results=await asyncio.gather(*[
        journal.apply_runtime(original,cumulative_units=20,apply=apply) for _ in range(8)])
    assert sum(r.applied_units for r in results)==20 and all(r.released for r in results)
    assert len(calls)==1
    async with journal.sessions() as db:
        account=await db.get(PaperAccount,1)
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert float(account.cash)==20095.9
        assert row.runtime_accounted_units==20 and row.unresolved_figi is None
    assert await journal.reserve(replace(original,client_order_id='SECOND'))


@pytest.mark.asyncio
async def test_failed_runtime_effect_rolls_back_effect_and_acknowledgement(pg_engine):
    journal,original=await setup(pg_engine)
    await observe(journal,original,2,'FILLED')
    await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                         evidence=evidence(original),defer_runtime=True)
    async def fail(db,saved,trades):
        await db.execute(update(PaperAccount).where(PaperAccount.id==1).values(cash=123))
        raise RuntimeError('OFFLINE interruption after database effect')
    with pytest.raises(RuntimeError,match='interruption'):
        await journal.apply_runtime(original,cumulative_units=20,apply=fail)
    async with journal.sessions() as db:
        assert float((await db.get(PaperAccount,1)).cash)==20000
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert row.runtime_accounted_units==0 and row.unresolved_figi=='FIGI'
    assert not await journal.reserve(replace(original,client_order_id='SECOND'))


@pytest.mark.asyncio
async def test_runtime_reconciliation_records_late_trade_and_retains_durable_pending(pg_engine,monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls,tz=None):
            return NOW+timedelta(hours=1)
    monkeypatch.setattr('app.bot.runtime.datetime',FixedClock)
    journal,original=await setup(pg_engine)
    rt=runtime(pg_engine)
    assert await rt._restore_close_intents()==1
    state=OrderState(order_id='EXCHANGE',order_request_id=original.client_order_id,
        figi=original.figi,direction=2,lots_requested=2,lots_executed=2,
        execution_report_status=1,currency='rub',order_date=NOW,
        stages=[OrderStage(trade_id='FIRST',quantity=2,
            price=MoneyValue(currency='rub',units=105,nano=0),execution_time=NOW+timedelta(minutes=1))])
    rt.broker.order_state=AsyncMock(return_value=state)
    await rt._reconcile_pending_orders()
    await rt._reconcile_pending_orders()
    assert rt.pending_orders['FIGI'].meta.get('durable_accounted_units')==20, rt.events.log.call_args_list
    assert rt.pending_orders['FIGI'].meta['runtime_accounting_pending']
    assert rt.entries_paused
    async with journal.sessions() as db:
        assert len((await db.scalars(select(PaperTrade))).all())==1
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert row.accounted_units==20 and row.runtime_accounted_units==0 and row.unresolved_figi=='FIGI'
    await pg_engine.dispose()
    restored=runtime(pg_engine)
    assert await restored._restore_close_intents()==1
    assert restored.pending_orders['FIGI'].id==original.client_order_id
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_existing_accounting_without_runtime_checkpoint_cannot_be_replayed_as_new(pg_engine):
    journal,original=await setup(pg_engine)
    await observe(journal,original,1,'PARTIAL_FILLED')
    await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,evidence=evidence(original,1))
    await observe(journal,original,2,'FILLED')
    with pytest.raises(ValueError,match='Cannot infer runtime acknowledgement'):
        await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                             evidence=evidence(original),defer_runtime=True)
    async with journal.sessions() as db:
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert row.accounted_units==10 and row.runtime_accounted_units is None
        assert len((await db.scalars(select(PaperTrade))).all())==1
