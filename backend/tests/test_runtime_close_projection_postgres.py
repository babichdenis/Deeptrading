from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from t_tech.invest.schemas import MoneyValue, OrderStage, OrderState

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from app.models.paper import PaperTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_close_position_projection_postgres import linked_position
from tests.test_close_stage_settlement_postgres import NOW
from tests.test_runtime_close_recovery_postgres import runtime


def state(count=2,cancelled=False):
    return OrderState(order_id='EXCHANGE',order_request_id='REQUEST_1',figi='FIGI',direction=2,
        lots_requested=2,lots_executed=count,execution_report_status=3 if cancelled else 1 if count==2 else 5,
        currency='rub',order_date=NOW,stages=[OrderStage(trade_id=f'STAGE_{index}',quantity=1,
            price=MoneyValue(currency='rub',units=price,nano=0),execution_time=NOW+timedelta(minutes=index))
            for index,price in enumerate((102,108),1)][:count])


async def initialized(engine,monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW+timedelta(hours=1)
    monkeypatch.setattr('app.bot.runtime.datetime',FixedClock)
    journal,original=await linked_position(engine)
    rt=runtime(engine)
    rt.config=SimpleNamespace(daily_loss_limit=1000)
    rt._bot_now=lambda:NOW+timedelta(hours=1)
    for name in ('_exit_plans','_exit_side','_exit_entry_px','_exit_qty','_trail_active','_trail_stop',
                 '_exit_target','_pos_leverage','_gb_peak','_opposite_count','_entry_bar_index'):
        setattr(rt,name,{'FIGI':20})
    rt._held={'FIGI'}
    rt._swing={'FIGI'}
    rt._last_exit_bar={}
    rt._bar_counter=123
    rt.broker.get_position=AsyncMock(return_value=None)
    rt.broker.order_state=AsyncMock(return_value=state())
    await rt._restore_close_intents()
    return journal,original,rt


def residual(**changes):
    values=dict(figi='FIGI',qty=10,side='LONG',entry_price=100)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_runtime_full_close_projects_once_and_clears_memory_after_broker_flat(pg_engine,monkeypatch):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    await rt._reconcile_pending_orders()
    assert 'FIGI' not in rt.pending_orders and 'FIGI' not in rt._held
    assert 'FIGI' not in rt._exit_plans and rt._last_exit_bar['FIGI']==123
    async with journal.sessions() as db:
        local=await db.get(SandboxTrade,7)
        ledger=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert local.qty==20 and local.exit_price==105 and local.exit_time is not None
        assert ledger.runtime_accounted_units==20 and ledger.unresolved_figi is None
    await rt._reconcile_pending_orders()
    assert rt.broker.order_state.await_count==1
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled',[False,True])
async def test_runtime_partial_preserves_protection_and_handles_terminal_remainder(pg_engine,monkeypatch,cancelled):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    rt.broker.order_state.return_value=state(1)
    rt.broker.get_position.return_value=residual()
    await rt._reconcile_pending_orders()
    assert rt.pending_orders['FIGI'].status=='PARTIAL_FILLED'
    assert rt._exit_qty['FIGI']==10 and rt._exit_plans['FIGI']==20 and 'FIGI' in rt._held
    rt.broker.order_state.return_value=state(1,True) if cancelled else state(2)
    rt.broker.get_position.return_value=residual() if cancelled else None
    await rt._reconcile_pending_orders()
    assert 'FIGI' not in rt.pending_orders
    assert ('FIGI' in rt._held)==cancelled
    async with journal.sessions() as db:
        rows=(await db.scalars(select(SandboxTrade))).all()
        assert sum(row.qty for row in rows)==20
        opened=[row for row in rows if row.exit_time is None]
        assert len(opened)==int(cancelled)
        if cancelled:assert opened[0].qty==10 and opened[0].stop_loss==95


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['still_present','wrong_quantity','wrong_side','wrong_price','timeout','scope'])
async def test_uncertain_broker_inventory_retains_pending_and_protection(pg_engine,monkeypatch,case):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    rt.broker.order_state.return_value=state(1) if case in ('wrong_quantity','wrong_side','wrong_price') else state()
    if case=='still_present':rt.broker.get_position.return_value=residual(qty=20)
    if case=='wrong_quantity':rt.broker.get_position.return_value=residual(qty=20)
    if case=='wrong_side':rt.broker.get_position.return_value=residual(side='SHORT')
    if case=='wrong_price':rt.broker.get_position.return_value=residual(entry_price=101)
    if case=='timeout':rt.broker.get_position.side_effect=TimeoutError('OFFLINE')
    if case=='scope':
        async def change(*args):
            rt.broker._account='OTHER'
            return None
        rt.broker.get_position.side_effect=change
    await rt._reconcile_pending_orders()
    assert 'FIGI' in rt.pending_orders and 'FIGI' in rt._held and 'FIGI' in rt._exit_plans
    async with journal.sessions() as db:
        ledger=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        local=await db.get(SandboxTrade,7)
        assert ledger.runtime_accounted_units==0 and ledger.unresolved_figi=='FIGI'
        assert local.qty==20 and local.exit_time is None


@pytest.mark.asyncio
async def test_retry_after_database_commit_finishes_memory_without_duplicate_trade(pg_engine,monkeypatch):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    cleanup=rt._clear_exit_state
    rt._clear_exit_state=Mock(side_effect=RuntimeError('OFFLINE interrupted after database commit'))
    await rt._reconcile_pending_orders()
    assert 'FIGI' in rt.pending_orders and 'FIGI' in rt._held
    rt._clear_exit_state=cleanup
    await rt._reconcile_pending_orders()
    assert 'FIGI' not in rt.pending_orders and 'FIGI' not in rt._held
    async with journal.sessions() as db:
        assert len((await db.scalars(select(PaperTrade))).all())==1
        assert len((await db.scalars(select(SandboxTrade))).all())==1
    rt.broker._api_post_order.assert_not_called()
