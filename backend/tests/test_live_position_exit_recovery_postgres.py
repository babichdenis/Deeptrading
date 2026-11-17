import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_open_completion_postgres import initialized


async def completed(engine):
    journal, original, rt = await initialized(engine)
    await rt._reconcile_pending_orders()
    assert not rt.pending_orders
    rt._exit_plans.clear()
    rt._exit_target = {'FIGI': 999}
    rt.config.sl_mode = 'fixed'
    rt.config.stop_pct = 0.5
    rt.broker.positions = AsyncMock(return_value=[SimpleNamespace(figi='FIGI', side='LONG', qty=20, entry_price=105)])
    return journal, original, rt


@pytest.mark.asyncio
async def test_completed_position_recovers_saved_policy_and_current_trailing_levels(pg_engine):
    journal, original, rt = await completed(pg_engine)
    async with journal.sessions() as db:
        row = (await db.scalars(select(SandboxTrade))).one()
        row.stop_loss = 101
        row.take_profit = None
        row.trailing_active = True
        await db.commit()
    result = await rt._restore_live_position_exits()
    assert result == dict(restored=['FIGI'], pending=[])
    assert rt._exit_plans['FIGI'] == original.exit_policy.restore()
    assert rt._trail_stop['FIGI'] == 101 and rt._trail_active['FIGI']
    assert 'FIGI' not in rt._exit_target
    assert not rt.entries_paused


@pytest.mark.asyncio
async def test_other_account_and_mode_rows_cannot_supply_protection(pg_engine):
    journal, original, rt = await completed(pg_engine)
    async with journal.sessions() as db:
        source = (await db.scalars(select(SandboxTrade))).one()
        source.broker_account_id = 'OTHER'
        db.add(SandboxTrade(mode='sandbox', broker_account_id=original.broker_account_id, figi='FIGI',
            ticker='FOREIGN', side='BUY', qty=20, entry_price=105, entry_time=source.entry_time,
            stop_loss=1, take_profit=999, meta=source.meta))
        await db.commit()
    result = await rt._restore_live_position_exits()
    assert result == dict(restored=[], pending=['FIGI'])
    assert 'FIGI' not in rt._exit_plans and rt.entries_paused
    await rt._ensure_exit_state('FIGI', SimpleNamespace(), rt.broker.positions.return_value[0])
    assert 'FIGI' not in rt._exit_plans


@pytest.mark.asyncio
async def test_unknown_policy_does_not_prevent_other_position_recovery(pg_engine):
    journal, original, rt = await completed(pg_engine)
    async with journal.sessions() as db:
        source = (await db.scalars(select(SandboxTrade))).one()
        known_meta = source.meta
        source.meta = '{}'
        db.add(SandboxTrade(mode='live', broker_account_id=original.broker_account_id, figi='GOOD',
            ticker='GOOD', side='BUY', qty=20, entry_price=105, entry_time=source.entry_time,
            stop_loss=100, take_profit=120, meta=known_meta))
        await db.commit()
    rt.broker.positions.return_value.append(SimpleNamespace(figi='GOOD', side='LONG', qty=20, entry_price=105))
    result = await rt._restore_live_position_exits()
    assert result == dict(restored=['GOOD'], pending=['FIGI'])
    assert set(rt._exit_plans) == {'GOOD'} and rt.entries_paused
    assert rt._held == {'GOOD', 'FIGI'}


@pytest.mark.asyncio
async def test_inventory_failure_preserves_known_holdings_and_blocks_entry(pg_engine):
    journal, original, rt = await completed(pg_engine)
    rt.broker.positions.side_effect = RuntimeError('portfolio unavailable')
    assert await rt._restore_live_position_exits() == dict(restored=[], pending=None)
    assert rt._held == {'FIGI'} and rt.entries_paused


@pytest.mark.asyncio
async def test_account_change_during_inventory_installs_nothing(pg_engine):
    journal, original, rt = await completed(pg_engine)
    positions = rt.broker.positions.return_value
    async def changed():
        rt.broker._account = 'OTHER'
        return positions
    rt.broker.positions.side_effect = changed
    assert await rt._restore_live_position_exits() == dict(restored=[], pending=None)
    assert not rt._exit_plans and rt.entries_paused


@pytest.mark.asyncio
async def test_duplicate_local_rows_are_not_resolved_by_latest_timestamp(pg_engine):
    journal, original, rt = await completed(pg_engine)
    async with journal.sessions() as db:
        source = (await db.scalars(select(SandboxTrade))).one()
        db.add(SandboxTrade(mode=source.mode, broker_account_id=source.broker_account_id, figi=source.figi,
            ticker=source.ticker, side=source.side, qty=source.qty, entry_price=source.entry_price,
            entry_time=source.entry_time, stop_loss=1, take_profit=999, meta=source.meta))
        await db.commit()
    assert await rt._restore_live_position_exits() == dict(restored=[], pending=['FIGI'])
    assert not rt._exit_plans and rt.entries_paused


@pytest.mark.asyncio
async def test_confirmed_empty_inventory_and_closed_local_row_clear_only_recovery_pause(pg_engine):
    journal, original, rt = await completed(pg_engine)
    rt._entry_pause().set_blocker('position_recovery_failed', True, 'live', original.broker_account_id, 'FIGI')
    rt.entries_paused = True
    async with journal.sessions() as db:
        source = (await db.scalars(select(SandboxTrade))).one()
        source.exit_time = rt._bot_now()
        await db.commit()
    rt.broker.positions.return_value = []
    assert await rt._restore_live_position_exits() == dict(restored=[], pending=[])
    assert rt._held == set() and rt._entry_pause().reasons == ['manual']
