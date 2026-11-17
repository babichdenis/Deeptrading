from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.sandbox_trade import SandboxTrade
from app.services.broker_order_journal import BrokerOrderJournal
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_open_fill_accounting_postgres import setup, account
from tests.test_open_order_journal_postgres import opening
from tests.test_runtime_close_recovery_postgres import runtime
from tests.test_runtime_close_projection_postgres import state
from tests.test_close_stage_settlement_postgres import NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('accounted', [False, True])
async def test_restart_restores_original_open_without_post(pg_engine, accounted):
    journal, original = await setup(pg_engine)
    assert await journal.reserve(opening(mode='sandbox', figi='SANDBOX_ONLY'))
    assert await journal.reserve(opening(broker_account_id='OTHER', figi='OTHER_ONLY'))
    if accounted: await account(journal, original, 1, 'PARTIAL_FILLED')
    await pg_engine.dispose()
    rt = runtime(pg_engine)
    assert await rt._restore_open_intents() == 1
    order = rt.pending_orders['FIGI']
    assert order.id == original.client_order_id and order.action == 'open'
    assert order.status == 'PENDING_RECONCILIATION' and rt.entries_paused
    assert order.meta['requested_lots'] == 2 and order.meta['lot_size'] == 10
    assert order.meta['entry_plan'] == original.entry_payload()
    assert order.meta['confirmed_executed_lots'] == (1 if accounted else 0)
    assert await rt._restore_open_intents() == 1
    assert rt.pending_orders['FIGI'] is order
    assert not await rt._execute_pending('FIGI', SimpleNamespace())
    rt.broker._api_post_order.assert_not_called()
    rt.broker._get_services.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, 1, 2])
async def test_recovered_open_reconciliation_records_only_confirmed_fill(pg_engine, count):
    journal, original = await setup(pg_engine)
    rt = runtime(pg_engine)
    rt._bot_now = lambda: NOW + timedelta(hours=1)
    await rt._restore_open_intents()
    response = state(count=count, cancelled=count == 0)
    response.direction = 1
    rt.broker.order_state = AsyncMock(return_value=response)
    await rt._reconcile_pending_orders()
    row = await journal.load(original)
    if count == 0:
        assert not rt.pending_orders and not rt.entries_paused
        assert row.unresolved_figi is None and row.accounted_units == 0
    else:
        assert rt.entries_paused and 'FIGI' in rt.pending_orders
        assert row.accounted_units == count * 10 and row.runtime_accounted_units == 0
        await rt._reconcile_pending_orders()
        async with journal.sessions() as db:
            position = (await db.scalars(select(SandboxTrade))).one()
            assert position.qty == count * 10
            assert position.entry_time == NOW + timedelta(minutes=1)
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_scope_change_during_restore_installs_nothing(pg_engine, monkeypatch):
    journal, original = await setup(pg_engine)
    rt = runtime(pg_engine)
    unresolved = BrokerOrderJournal.unresolved
    async def changed(self, **kwargs):
        result = await unresolved(self, **kwargs)
        rt.broker._account = 'OTHER'
        return result
    monkeypatch.setattr(BrokerOrderJournal, 'unresolved', changed)
    with pytest.raises(RuntimeError, match='opening_journal_recovery_failed'):
        await rt._restore_open_intents()
    assert rt.pending_orders == {} and rt.entries_paused


@pytest.mark.asyncio
async def test_open_recovery_missing_storage_fails_closed(pg_engine):
    rt = runtime(pg_engine)
    with pytest.raises(RuntimeError, match='opening_journal_recovery_failed'):
        await rt._restore_open_intents()
    assert rt.entries_paused and rt.pending_orders == {}


@pytest.mark.asyncio
async def test_failed_zero_fill_observation_keeps_pending_and_pause(pg_engine, monkeypatch):
    journal, original = await setup(pg_engine)
    rt = runtime(pg_engine)
    await rt._restore_open_intents()
    response = state(count=0, cancelled=True)
    response.direction = 1
    rt.broker.order_state = AsyncMock(return_value=response)
    monkeypatch.setattr(BrokerOrderJournal, 'observe', AsyncMock(side_effect=RuntimeError('storage unavailable')))
    await rt._reconcile_pending_orders()
    assert 'FIGI' in rt.pending_orders and rt.entries_paused
    assert (await journal.load(original)).unresolved_figi == 'FIGI'


@pytest.mark.asyncio
async def test_terminal_empty_cannot_clear_manual_pause(pg_engine):
    journal, original = await setup(pg_engine)
    rt = runtime(pg_engine)
    await rt._restore_open_intents()
    rt.entries_paused = True
    response = state(count=0, cancelled=True)
    response.direction = 1
    rt.broker.order_state = AsyncMock(return_value=response)
    await rt._reconcile_pending_orders()
    assert rt.pending_orders == {} and rt.entries_paused
    assert rt._entry_pause().reasons == ['manual']
