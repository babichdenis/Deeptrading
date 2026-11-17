from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime
from app.services.broker_order_journal import BrokerOrderJournal
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine, prepare, apply_migration, intent, delta_trade


def runtime(engine):
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    broker=LiveBroker.__new__(LiveBroker)
    broker.mode='live'
    broker._account='OFFLINE_ACCOUNT'
    broker.sessions=async_sessionmaker(engine,expire_on_commit=False)
    broker.flush_portfolio=AsyncMock()
    broker.get_position=AsyncMock(side_effect=AssertionError('position read must be explicitly mocked'))
    broker._api_post_order=Mock(side_effect=AssertionError('recovery must never submit'))
    broker._get_services=Mock(side_effect=AssertionError('recovery must not contact SDK'))
    rt.broker=broker
    rt.broker_mode='live'
    rt.pending_orders={}
    rt.tickers={}
    rt.entries_paused=False
    rt.events=SimpleNamespace(log=Mock())
    rt._log=lambda *a,**k:None
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize('accounted',[False,True])
async def test_restore_scoped_durable_close_before_any_resend(pg_engine,accounted):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    journal=BrokerOrderJournal(async_sessionmaker(pg_engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    assert await journal.reserve(intent(mode='sandbox',figi='SANDBOX_ONLY'))
    assert await journal.reserve(intent(broker_account_id='OTHER',figi='OTHER_ONLY'))
    assert await journal.observe(intent(),broker_order_id='EXCHANGE',broker_status='PARTIAL_FILLED',executed_units=10)
    if accounted:
        await journal.settle(intent(),broker_order_id='EXCHANGE',cumulative_units=10,trade=delta_trade(10))
    await pg_engine.dispose()
    rt=runtime(pg_engine)
    assert await rt._restore_close_intents()==1
    assert set(rt.pending_orders)=={'FIGI'} and rt.entries_paused
    order=rt.pending_orders['FIGI']
    assert order.id=='REQUEST_1' and order.action=='close'
    assert order.meta['requested_units']==20 and order.meta['requested_lots']==2 and order.meta['lot_size']==10
    assert order.meta['observed_executed_units']==10
    assert order.qty==(10 if accounted else 20)
    assert order.status==('PARTIAL_FILLED' if accounted else 'PENDING_RECONCILIATION')
    assert await rt._execute_pending('FIGI',SimpleNamespace()) is False
    assert rt.pending_orders['FIGI'] is order
    assert (await rt._submit_close_reconciled('FIGI','OFFLINE',20,99,'OFFLINE'))['status']=='blocked_existing'
    assert await rt._restore_close_intents()==1
    assert rt.pending_orders['FIGI'] is order
    rt.broker._api_post_order.assert_not_called()
    rt.broker._get_services.assert_not_called()


@pytest.mark.asyncio
async def test_missing_journal_aborts_recovery_and_pauses_entries(pg_engine):
    rt=runtime(pg_engine)
    with pytest.raises(RuntimeError,match='close_journal_recovery_failed'):
        await rt._restore_close_intents()
    assert rt.entries_paused and rt.pending_orders=={}
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_scope_change_during_load_does_not_install_old_account_orders(pg_engine,monkeypatch):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    journal=BrokerOrderJournal(async_sessionmaker(pg_engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    rt=runtime(pg_engine)
    original=BrokerOrderJournal.unresolved
    async def changed(journal,**kwargs):
        rows=await original(journal,**kwargs)
        rt.broker._account='OTHER'
        return rows
    monkeypatch.setattr(BrokerOrderJournal,'unresolved',changed)
    with pytest.raises(RuntimeError,match='close_journal_recovery_failed'):
        await rt._restore_close_intents()
    assert rt.entries_paused and rt.pending_orders=={}


@pytest.mark.asyncio
async def test_existing_different_intent_is_preserved_on_recovery_conflict(pg_engine):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    journal=BrokerOrderJournal(async_sessionmaker(pg_engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    rt=runtime(pg_engine)
    existing=SimpleNamespace(id='OTHER_REQUEST',action='open',meta={})
    rt.pending_orders['FIGI']=existing
    with pytest.raises(RuntimeError,match='close_journal_recovery_failed'):
        await rt._restore_close_intents()
    assert rt.pending_orders['FIGI'] is existing and rt.entries_paused


@pytest.mark.asyncio
@pytest.mark.parametrize('paused',[False,True])
async def test_empty_recovery_preserves_existing_entry_pause(pg_engine,paused):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    rt=runtime(pg_engine)
    rt.entries_paused=paused
    assert await rt._restore_close_intents()==0
    assert rt.entries_paused is paused and rt.pending_orders=={}
    rt.broker._get_services.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,executed,released',[(3,0,True),(2,0,True),(4,0,False),(5,1,False),(1,2,False)])
async def test_reconciliation_commits_broker_evidence_before_memory_cleanup(pg_engine,status,executed,released):
    from app.models.broker_order_intent import BrokerOrderIntent
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    journal=BrokerOrderJournal(async_sessionmaker(pg_engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    rt=runtime(pg_engine)
    await rt._restore_close_intents()
    order=rt.pending_orders['FIGI']
    rt._held={'FIGI'}
    rt._exit_plans={'FIGI':object()}
    rt.broker.order_state=AsyncMock(return_value=SimpleNamespace(execution_report_status=status,lots_executed=executed,order_id='EXCHANGE'))
    await rt._reconcile_pending_orders()
    async with rt.broker.sessions() as db:
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert row.observed_units==executed*10
        assert (row.unresolved_figi is None)==released
    assert ('FIGI' not in rt.pending_orders)==released
    assert 'FIGI' in rt._held and 'FIGI' in rt._exit_plans
    if released:
        order.status='PENDING_RECONCILIATION'
        rt.pending_orders['FIGI']=order
        await rt._reconcile_pending_orders()
        assert 'FIGI' not in rt.pending_orders
        assert await journal.reserve(intent(client_order_id='NEXT'))
    else:
        assert not await journal.reserve(intent(client_order_id='NEXT'))


@pytest.mark.asyncio
async def test_failed_observation_keeps_memory_and_database_pending(pg_engine,monkeypatch):
    from app.models.broker_order_intent import BrokerOrderIntent
    await prepare(pg_engine)
    async with pg_engine.begin() as db:await db.run_sync(apply_migration)
    journal=BrokerOrderJournal(async_sessionmaker(pg_engine,expire_on_commit=False))
    assert await journal.reserve(intent())
    rt=runtime(pg_engine)
    await rt._restore_close_intents()
    rt.broker.order_state=AsyncMock(return_value=SimpleNamespace(execution_report_status=3,lots_executed=0,order_id='EXCHANGE'))
    with monkeypatch.context() as patch:
        patch.setattr(BrokerOrderJournal,'observe',AsyncMock(side_effect=RuntimeError('OFFLINE storage failure')))
        await rt._reconcile_pending_orders()
    assert rt.pending_orders['FIGI'].meta['durable_observation_pending'] and rt.entries_paused
    async with rt.broker.sessions() as db:
        row=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert row.unresolved_figi=='FIGI'
    await rt._reconcile_pending_orders()
    assert 'FIGI' not in rt.pending_orders
