import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from t_tech.invest.schemas import MoneyValue, OrderExecutionReportStatus as Status, PostOrderResponse

from app.bot.live_broker import LiveBroker, LivePosition
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine, prepare, apply_migration


ENTRY_TIME=datetime(2020,1,1,tzinfo=timezone.utc)


async def adapter(engine,cluster,*,outcome='full',prepare_schema=True):
    if prepare_schema:
        await prepare(engine)
        async with engine.begin() as db:
            await db.run_sync(apply_migration)
            await db.execute(text("UPDATE paper_accounts SET name='default' WHERE id=1"))
    async with engine.connect() as db:
        schema=await db.scalar(text('SELECT current_schema()'))
    broker=LiveBroker.__new__(LiveBroker)
    broker.mode='sandbox'
    broker._account='OFFLINE_ACCOUNT'
    broker.sessions=async_sessionmaker(engine,expire_on_commit=False)
    broker.config=SimpleNamespace(commission_rate=0.0005,use_margin=False)
    broker._log=lambda *a,**k:None
    position=LivePosition('FIGI','OFFLINE','LONG',20,100,ENTRY_TIME,None,None)
    broker.get_position=AsyncMock(return_value=position)
    broker._get_lot=AsyncMock(return_value=10)
    calls=[]
    async def committed_reservation(request):
        db=await asyncpg.connect(user='journal_test',host='127.0.0.1',port=cluster.port,database='postgres',
                                server_settings={'search_path':schema})
        try:
            row=await db.fetchrow('SELECT client_order_id,requested_units,position_snapshot FROM broker_order_intents WHERE mode=$1 AND broker_account_id=$2 AND client_order_id=$3',
                                 'sandbox','OFFLINE_ACCOUNT',request['order_id'])
            assert row is not None and row['requested_units']==20 and row['position_snapshot'] is not None
        finally:
            await db.close()
    def post(**request):
        asyncio.run(committed_reservation(request))
        calls.append(request)
        if outcome=='timeout':raise TimeoutError('OFFLINE unknown submission result')
        executed=0 if outcome=='new' else 1 if outcome=='partial' else 2
        status=Status.EXECUTION_REPORT_STATUS_NEW if outcome=='new' else Status.EXECUTION_REPORT_STATUS_PARTIALLYFILL if outcome=='partial' else Status.EXECUTION_REPORT_STATUS_FILL
        return PostOrderResponse(order_id='EXCHANGE',order_request_id='OTHER' if outcome=='wrong_request' else request['order_id'],
                                 figi=request['instrument_id'],direction=request['direction'],
                                 lots_requested=request['quantity'],lots_executed=executed,execution_report_status=status,
                                 executed_order_price=MoneyValue(currency='rub',units=99,nano=0))
    broker._api_post_order=post
    return broker,calls


def request_kwargs(request_id='f4df8d3b-1140-4128-a62c-de562129b7fe',known_entry=True):
    return dict(client_order_id=request_id,expected_units=20,
                entry_time=ENTRY_TIME if known_entry else None,
                entry_time_source='broker_history' if known_entry else 'unknown')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['full','partial','new','wrong_request'])
async def test_actual_adapter_and_postgres_journal_share_execution_transaction(pg_engine,isolated_postgres,outcome):
    broker,calls=await adapter(pg_engine,isolated_postgres,outcome=outcome)
    trade=await broker.close_position('FIGI',99,'OFFLINE',**request_kwargs())
    assert len(calls)==1 and calls[0]['quantity']==2
    async with broker.sessions() as db:
        row=await db.get(BrokerOrderIntent,('sandbox','OFFLINE_ACCOUNT','f4df8d3b-1140-4128-a62c-de562129b7fe'))
        count=await db.scalar(select(func.count()).select_from(PaperTrade))
        fill_count=await db.scalar(select(func.count()).select_from(BrokerOrderFill))
    observed={'full':20,'partial':10}.get(outcome,0)
    assert row.accounted_units==0 and count==fill_count==0
    assert row.observed_units==observed
    assert (trade.observed_units if trade else 0)==observed
    assert row.unresolved_figi=='FIGI'
    await pg_engine.dispose()
    restored,repeated_calls=await adapter(pg_engine,isolated_postgres,outcome=outcome,prepare_schema=False)
    next_id='f4df8d3b-1140-4128-a62c-de562129b7fe' if outcome=='full' else '14c5f452-7c0e-4602-836e-d716c851bacc'
    assert await restored.close_position('FIGI',99,'OFFLINE',**request_kwargs(next_id)) is None
    assert repeated_calls==[]


@pytest.mark.asyncio
async def test_timeout_survives_adapter_recreation_without_second_submission(pg_engine,isolated_postgres):
    broker,calls=await adapter(pg_engine,isolated_postgres,outcome='timeout')
    with pytest.raises(TimeoutError):
        await broker.close_position('FIGI',99,'OFFLINE',**request_kwargs())
    assert len(calls)==1
    await pg_engine.dispose()
    restored,repeated_calls=await adapter(pg_engine,isolated_postgres,prepare_schema=False)
    assert await restored.close_position('FIGI',99,'OFFLINE',**request_kwargs('14c5f452-7c0e-4602-836e-d716c851bacc')) is None
    assert repeated_calls==[]
    async with restored.sessions() as db:
        row=await db.get(BrokerOrderIntent,('sandbox','OFFLINE_ACCOUNT','f4df8d3b-1140-4128-a62c-de562129b7fe'))
        assert row.unresolved_figi=='FIGI' and row.accounted_units==0


@pytest.mark.asyncio
async def test_observation_failure_after_post_keeps_execution_pending_without_trade(pg_engine,isolated_postgres,monkeypatch):
    broker,calls=await adapter(pg_engine,isolated_postgres)
    from app.services.broker_order_journal import BrokerOrderJournal
    monkeypatch.setattr(BrokerOrderJournal,'observe',AsyncMock(side_effect=RuntimeError('OFFLINE observation failure')))
    assert await broker.close_position('FIGI',99,'OFFLINE',**request_kwargs()) is None
    assert len(calls)==1
    async with broker.sessions() as db:
        row=await db.get(BrokerOrderIntent,('sandbox','OFFLINE_ACCOUNT','f4df8d3b-1140-4128-a62c-de562129b7fe'))
        assert row.observed_units==0 and row.accounted_units==0 and row.unresolved_figi=='FIGI'
        assert await db.scalar(select(func.count()).select_from(PaperTrade))==0
    assert await broker.close_position('FIGI',99,'OFFLINE',**request_kwargs('14c5f452-7c0e-4602-836e-d716c851bacc')) is None
    assert len(calls)==1


@pytest.mark.asyncio
async def test_unknown_entry_time_does_not_create_invented_trade_after_execution(pg_engine,isolated_postgres):
    broker,calls=await adapter(pg_engine,isolated_postgres)
    receipt=await broker.close_position('FIGI',99,'OFFLINE',**request_kwargs(known_entry=False))
    assert receipt is not None and receipt.observed_units==20
    assert len(calls)==1
    async with broker.sessions() as db:
        row=await db.get(BrokerOrderIntent,('sandbox','OFFLINE_ACCOUNT','f4df8d3b-1140-4128-a62c-de562129b7fe'))
        assert row.position_snapshot['entry_time'] is None
        assert row.observed_units==20 and row.accounted_units==0 and row.unresolved_figi=='FIGI'
        assert await db.scalar(select(func.count()).select_from(PaperTrade))==0
