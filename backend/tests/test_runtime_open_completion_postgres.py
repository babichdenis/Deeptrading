from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_open_fill_accounting_postgres import setup
from tests.test_runtime_close_recovery_postgres import runtime
from tests.test_runtime_close_projection_postgres import state
from tests.test_close_stage_settlement_postgres import NOW


async def initialized(engine):
    journal, original = await setup(engine)
    rt = runtime(engine)
    rt.config = SimpleNamespace(daily_loss_limit=1000)
    rt._bot_now = lambda: NOW + timedelta(hours=1)
    rt._held = set()
    rt._bar_counter = 50
    for name in ('_exit_plans', '_exit_side', '_exit_qty', '_exit_entry_px', '_trail_active', '_trail_stop',
                 '_exit_target', '_entry_bar_index', '_pos_leverage', '_peak_pnl', '_gb_peak', '_trail_info', '_held_since'):
        setattr(rt, name, {})
    response = state()
    response.direction = 1
    rt.broker.order_state = AsyncMock(return_value=response)
    rt.broker.get_position = AsyncMock(return_value=SimpleNamespace(figi='FIGI', side='LONG', qty=20, entry_price=105))
    await rt._restore_open_intents()
    return journal, original, rt


@pytest.mark.asyncio
async def test_full_open_restores_original_protection_then_releases(pg_engine):
    journal, original, rt = await initialized(pg_engine)
    order = rt.pending_orders['FIGI']
    await rt._reconcile_pending_orders()
    assert order.qty == original.requested_units // original.lot_size
    assert order.meta['remaining_lots'] == 0
    assert rt.pending_orders == {} and not rt.entries_paused
    assert rt._held == {'FIGI'} and rt._exit_qty['FIGI'] == 20
    assert rt._trail_stop['FIGI'] == float(original.stop_loss)
    assert rt._exit_target['FIGI'] == float(original.take_profit)
    assert rt._exit_plans['FIGI'] == original.exit_policy.restore()
    assert rt._exit_plans['FIGI'].period == 21
    row = await journal.load(original)
    assert row.runtime_accounted_units == 20 and row.unresolved_figi is None
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'quantity', 'price', 'side', 'manual', 'scope', 'risk'])
async def test_completion_requires_inventory_and_preserves_independent_pauses(pg_engine, case):
    journal, original, rt = await initialized(pg_engine)
    position = rt.broker.get_position.return_value
    if case == 'missing': rt.broker.get_position.return_value = None
    elif case == 'quantity': position.qty = 10
    elif case == 'price': position.entry_price = 999
    elif case == 'side': position.side = 'SHORT'
    elif case == 'manual': rt.entries_paused = True
    elif case == 'scope':
        async def change(figi):
            rt.broker._account = 'OTHER'
            return position
        rt.broker.get_position.side_effect = change
    else: rt.refresh_daily_pnl = AsyncMock(side_effect=RuntimeError('risk unavailable'))
    await rt._reconcile_pending_orders()
    row = await journal.load(original)
    if case == 'manual':
        assert row.unresolved_figi is None and not rt.pending_orders
        assert rt.entries_paused and rt._entry_pause().reasons == ['manual']
    else:
        assert row.unresolved_figi == 'FIGI' and row.runtime_accounted_units == 0
        assert 'FIGI' in rt.pending_orders and rt.entries_paused
        if case == 'risk': assert rt._exit_qty['FIGI'] == 20


@pytest.mark.asyncio
async def test_partial_then_full_retains_one_position_and_updates_quantity(pg_engine):
    journal, original, rt = await initialized(pg_engine)
    partial = state(count=1)
    partial.direction = 1
    rt.broker.order_state.return_value = partial
    rt.broker.get_position.return_value.qty = 10
    rt.broker.get_position.return_value.entry_price = 102
    await rt._reconcile_pending_orders()
    assert rt._exit_qty['FIGI'] == 10 and rt.entries_paused
    assert rt.pending_orders['FIGI'].status == 'PARTIAL_FILLED'
    full = state()
    full.direction = 1
    rt.broker.order_state.return_value = full
    rt.broker.get_position.return_value.qty = 20
    rt.broker.get_position.return_value.entry_price = 105
    await rt._reconcile_pending_orders()
    assert rt._exit_qty['FIGI'] == 20 and not rt.pending_orders and not rt.entries_paused
    async with journal.sessions() as db:
        assert (await db.scalars(select(SandboxTrade))).one().qty == 20


@pytest.mark.asyncio
async def test_ack_commit_then_failure_finishes_on_retry_without_duplicate(pg_engine, monkeypatch):
    from app.services import open_fill_accounting
    journal, original, rt = await initialized(pg_engine)
    acknowledge = open_fill_accounting.acknowledge_open
    async def fail_after_commit(*args, **kwargs):
        await acknowledge(*args, **kwargs)
        raise RuntimeError('crash after commit before pending cleanup')
    monkeypatch.setattr(open_fill_accounting, 'acknowledge_open', fail_after_commit)
    await rt._reconcile_pending_orders()
    assert 'FIGI' in rt.pending_orders and rt.entries_paused
    row = await journal.load(original)
    assert row.status == 'SETTLED' and row.runtime_accounted_units == 20
    monkeypatch.setattr(open_fill_accounting, 'acknowledge_open', acknowledge)
    await rt._reconcile_pending_orders()
    assert rt.pending_orders == {} and not rt.entries_paused
    async with journal.sessions() as db:
        assert (await db.scalars(select(SandboxTrade))).one().qty == 20
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_partial_cancel_keeps_filled_position_and_releases_intent(pg_engine):
    journal, original, rt = await initialized(pg_engine)
    response = state(count=1, cancelled=True)
    response.direction = 1
    rt.broker.order_state.return_value = response
    rt.broker.get_position.return_value.qty = 10
    rt.broker.get_position.return_value.entry_price = 102
    await rt._reconcile_pending_orders()
    assert rt.pending_orders == {} and not rt.entries_paused
    assert rt._exit_qty['FIGI'] == 10 and rt._held == {'FIGI'}
    row = await journal.load(original)
    assert row.unresolved_figi is None and row.runtime_accounted_units == 10


@pytest.mark.asyncio
async def test_metadata_flag_cannot_replace_actual_runtime_protection(pg_engine):
    journal, original, rt = await initialized(pg_engine)
    rt.pending_orders['FIGI'].meta['protection_installed'] = True
    await rt._reconcile_pending_orders()
    assert rt._exit_plans['FIGI'] == original.exit_policy.restore()
    assert rt._trail_stop['FIGI'] == float(original.stop_loss)
    assert not rt.pending_orders


@pytest.mark.asyncio
async def test_late_open_fill_keeps_persisted_and_runtime_trailing(pg_engine, monkeypatch):
    from app.models.sandbox_trade import SandboxTrade

    journal, original, rt = await initialized(pg_engine)
    partial = state(count=1)
    partial.direction = 1
    rt.broker.order_state.return_value = partial
    rt.broker.get_position.return_value = SimpleNamespace(figi='FIGI', side='LONG', qty=10, entry_price=102)
    await rt._reconcile_pending_orders()
    monkeypatch.setattr('app.bot.runtime.SessionLocal', journal.sessions)
    rt._trail_stop['FIGI'] = 101.0
    rt._trail_active['FIGI'] = True
    rt._exit_target.pop('FIGI', None)
    await rt._st_update_sl('FIGI', 101.0, True)
    full = state(count=2)
    full.direction = 1
    rt.broker.order_state.return_value = full
    rt.broker.get_position.return_value = SimpleNamespace(figi='FIGI', side='LONG', qty=20, entry_price=105)
    await rt._reconcile_pending_orders()
    assert rt.pending_orders == {} and rt._exit_qty['FIGI'] == 20
    assert rt._trail_stop['FIGI'] == 101.0 and rt._trail_active['FIGI']
    assert rt._exit_target.get('FIGI') is None
    row = await journal.load(original)
    assert row.accounted_units == row.runtime_accounted_units == 20
    async with journal.sessions() as db:
        position = await db.get(SandboxTrade, row.entry_accounting['local_position_id'])
        assert position.stop_loss == 101 and position.trailing_active and position.take_profit is None
