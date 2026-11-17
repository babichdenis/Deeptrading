from datetime import timedelta

import pytest

from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_close_projection_postgres import initialized
from tests.test_close_stage_settlement_postgres import NOW


async def prior_trade(journal,**changes):
    values=dict(id=100,mode='live',broker_account_id='OFFLINE_ACCOUNT',figi='PRIOR',ticker='PRIOR',
        side='BUY',qty=1,entry_time=NOW-timedelta(hours=2),entry_price=100,
        exit_time=NOW-timedelta(hours=1),exit_price=99,net_pnl=-1001,exit_reason='STOP')
    values.update(changes)
    async with journal.sessions() as db:
        db.add(SandboxTrade(**values))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['normal','manual','other_account','other_mode','loss_limit','unknown'])
async def test_close_releases_only_its_pause_after_scoped_risk_refresh(pg_engine,monkeypatch,case):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    rt.config.daily_loss_limit=100
    if case=='manual':
        async def pause_during_inventory(figi):
            rt.entries_paused=True
            return None
        rt.broker.get_position.side_effect=pause_during_inventory
    if case=='other_account':await prior_trade(journal,broker_account_id='OTHER')
    if case=='other_mode':await prior_trade(journal,mode='sandbox')
    if case=='loss_limit':await prior_trade(journal)
    if case=='unknown':await prior_trade(journal,net_pnl=None)
    await rt._reconcile_pending_orders()
    if case=='unknown':
        assert rt.entries_paused and 'FIGI' in rt.pending_orders
        assert 'risk_evidence_unavailable' in rt._entry_pause().reasons
        async with journal.sessions() as db:
            row=await db.get(SandboxTrade,100)
            row.net_pnl=0
            await db.commit()
        await rt._reconcile_pending_orders()
        assert not rt.entries_paused and not rt.pending_orders
    else:
        assert not rt.pending_orders
        assert rt.entries_paused==(case in ('manual','loss_limit'))
        if case=='manual':assert rt._entry_pause().reasons==['manual']
        if case=='loss_limit':assert rt._entry_pause().reasons==['daily_loss_limit']
        if case in ('other_account','other_mode'):assert rt._daily_pnl_cache[1]>0


@pytest.mark.asyncio
async def test_account_change_during_risk_read_keeps_pending_and_does_not_cache_other_scope(pg_engine,monkeypatch):
    from contextlib import asynccontextmanager
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    sessions=rt.broker.sessions
    @asynccontextmanager
    async def changing_sessions():
        async with sessions() as db:
            execute=db.execute
            async def changing_execute(statement,*args,**kwargs):
                result=await execute(statement,*args,**kwargs)
                columns=getattr(statement,'selected_columns',())
                if [column.key for column in columns]==['net_pnl']:
                    rt.broker._account='OTHER'
                return result
            db.execute=changing_execute
            yield db
    rt.broker.sessions=changing_sessions
    await rt._reconcile_pending_orders()
    assert 'FIGI' in rt.pending_orders and 'FIGI' in rt._held and rt.entries_paused
    assert getattr(rt,'_daily_pnl_cache',None) is None
    rt.broker.sessions=sessions
    rt.broker._account='OFFLINE_ACCOUNT'
    await rt._reconcile_pending_orders()
    assert not rt.pending_orders and not rt.entries_paused


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_account', 'scoped', 'unknown'])
async def test_paper_risk_uses_bound_sessions_and_default_account(pg_engine, monkeypatch, case):
    from app.bot.paper_broker import PaperBroker
    from app.models.paper import PaperAccount
    from tests.test_broker_order_journal_postgres import delta_trade
    journal, original, rt = await initialized(pg_engine, monkeypatch)
    rt.broker_mode = 'paper'
    rt.broker = PaperBroker(journal.sessions)
    if case == 'missing_account':
        with pytest.raises(ValueError, match='paper_risk_account_missing'):
            await rt.refresh_daily_pnl()
        return
    async with journal.sessions() as db:
        db.add(PaperAccount(id=2, name='default', cash=20000, initial_cash=20000))
        await db.flush()
        db.add(delta_trade(1, account_id=1, net_pnl=-10000))
        db.add(delta_trade(1, account_id=2, net_pnl=float('nan') if case == 'unknown' else -5))
        await db.commit()
    if case == 'unknown':
        with pytest.raises(ValueError, match='daily_pnl_contains_unknown'):
            await rt.refresh_daily_pnl()
    else:
        assert await rt.refresh_daily_pnl() == -5
        assert rt.daily_pnl_cached() == -5
