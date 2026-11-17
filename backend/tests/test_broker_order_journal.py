import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.broker_order_intent import BrokerOrderIntent
from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent


def intent(**changes):
    base=CloseIntent('live','OFFLINE_ACCOUNT','REQUEST_1','FIGI','SELL',20,10,
                     datetime(2026,11,16,tzinfo=timezone.utc))
    return replace(base,**changes)


@pytest.mark.asyncio
async def test_reservation_survives_reopen_and_refuses_resubmission(tmp_path):
    url='sqlite+aiosqlite:///'+str(tmp_path/'orders.sqlite')
    engine=create_async_engine(url)
    async with engine.begin() as db:
        await db.run_sync(BrokerOrderIntent.__table__.create)
    journal=BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    await engine.dispose()
    engine=create_async_engine(url)
    journal=BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    rows=await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT')
    assert len(rows)==1 and rows[0].client_order_id=='REQUEST_1'
    assert rows[0].requested_units==20 and rows[0].lot_size==10
    assert not await journal.reserve(intent())
    assert not await journal.reserve(intent(client_order_id='REQUEST_2'))
    assert await journal.reserve(intent(mode='sandbox'))
    assert await journal.reserve(intent(broker_account_id='OFFLINE_OTHER'))
    assert len(await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT'))==1
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_reservation_allows_only_one_sender(tmp_path):
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'concurrent.sqlite'))
    async with engine.begin() as db:
        await db.run_sync(BrokerOrderIntent.__table__.create)
    journal=BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    results=await asyncio.gather(journal.reserve(intent()),journal.reserve(intent(client_order_id='REQUEST_2')))
    assert sorted(results)==[False,True]
    await engine.dispose()


@pytest.mark.asyncio
async def test_storage_failure_never_grants_submission(tmp_path):
    from sqlalchemy.exc import OperationalError
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'absent_schema.sqlite'))
    journal=BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    with pytest.raises(OperationalError):
        await journal.reserve(intent())
    await engine.dispose()


@pytest.mark.parametrize('changes',[{'requested_units':21},{'lot_size':0},{'side':'INVALID'},
    {'mode':'paper'},{'broker_account_id':''},{'created_at':datetime(2026,11,16)}])
def test_invalid_intent_cannot_be_persisted(changes):
    with pytest.raises(ValueError):intent(**changes).validate()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,units,released', [('CANCELLED',0,True),('REJECTED',0,True),
    ('CANCELLED',10,False),('FILLED',20,False)])
async def test_observation_releases_only_confirmed_empty_order(tmp_path,status,units,released):
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'observations.sqlite'))
    async with engine.begin() as db:await db.run_sync(BrokerOrderIntent.__table__.create)
    journal=BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    assert await journal.observe(intent(),broker_order_id='EXCHANGE',broker_status=status,executed_units=units)
    assert await journal.reserve(intent(client_order_id='NEXT'))==released
    rows=await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT')
    if released:
        assert await journal.observe(intent(),broker_order_id='EXCHANGE',broker_status=status,executed_units=units)
        assert not await journal.observe(intent(),broker_order_id='OTHER',broker_status=status,executed_units=units)
    if not released:
        assert rows[0].observed_units==units
        assert await journal.observe(intent(),broker_order_id='EXCHANGE',broker_status=status,executed_units=units)
        assert not await journal.observe(intent(),broker_order_id='EXCHANGE',broker_status='CANCELLED',executed_units=0)
        assert not await journal.observe(intent(),broker_order_id='OTHER',broker_status=status,executed_units=units)
        assert not await journal.observe(intent(side='BUY'),broker_order_id='EXCHANGE',broker_status=status,executed_units=units)
        assert (await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT'))[0].observed_units==units
    await engine.dispose()
