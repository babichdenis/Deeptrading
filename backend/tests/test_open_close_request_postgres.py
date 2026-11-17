import asyncio
from datetime import timedelta

import pytest

from app.services.open_close_request import CloseAfterOpen, request_close_after_open
from app.services.open_fill_accounting import acknowledge_open
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_open_fill_accounting_postgres import setup, account
from tests.test_open_order_journal_postgres import opening


@pytest.mark.asyncio
async def test_exit_request_survives_pool_restart_and_open_acknowledgement(pg_engine):
    journal, original = await setup(pg_engine)
    partial = await account(journal, original, 1, 'CANCELLED')
    request = CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3))
    assert await request_close_after_open(journal.sessions, original, request) == request
    await pg_engine.dispose()
    assert CloseAfterOpen.from_payload((await journal.load(original)).close_request) == request
    result = await acknowledge_open(journal.sessions, original, units=10, local_position_id=partial.local_position_id)
    assert result.released
    row = await journal.load(original)
    assert row.unresolved_figi is None and CloseAfterOpen.from_payload(row.close_request) == request


@pytest.mark.asyncio
async def test_concurrent_exit_reasons_preserve_first_committed_request(pg_engine):
    journal, original = await setup(pg_engine)
    requests = [CloseAfterOpen(reason, original.created_at + timedelta(minutes=3))
                for reason in ('STOP_LOSS', 'SESSION_CLOSE', 'MANUAL')]
    results = await asyncio.gather(*(request_close_after_open(journal.sessions, original, item) for item in requests))
    assert len(set(results)) == 1 and results[0] in requests
    row = await journal.load(original)
    assert row.observed_units == 0 and row.accounted_units == 0 and row.unresolved_figi == original.figi


@pytest.mark.asyncio
async def test_other_account_cannot_attach_exit_request(pg_engine):
    journal, original = await setup(pg_engine)
    request = CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3))
    with pytest.raises(ValueError, match='Original opening reservation'):
        await request_close_after_open(journal.sessions, opening(broker_account_id='OTHER'), request)
    assert (await journal.load(original)).close_request is None


@pytest.mark.asyncio
async def test_released_open_without_existing_exit_request_uses_normal_close_path(pg_engine):
    journal, original = await setup(pg_engine)
    full = await account(journal, original, 2, 'FILLED')
    await acknowledge_open(journal.sessions, original, units=20, local_position_id=full.local_position_id)
    request = CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3))
    with pytest.raises(ValueError, match='already released'):
        await request_close_after_open(journal.sessions, original, request)
    assert (await journal.load(original)).close_request is None


@pytest.mark.asyncio
async def test_recovery_includes_released_open_and_keeps_stable_close_identity(pg_engine):
    from app.services.open_close_request import load_close_after_open

    journal, original = await setup(pg_engine)
    partial = await account(journal, original, 1, 'CANCELLED')
    request = CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3))
    await request_close_after_open(journal.sessions, original, request)
    before = await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)
    await acknowledge_open(journal.sessions, original, units=10, local_position_id=partial.local_position_id)
    await pg_engine.dispose()
    after = await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)
    assert before == after and len(after) == 1
    assert after[0].intent == original and after[0].request == request
    assert before[0].close_client_order_id == after[0].close_client_order_id
    assert after[0].close_client_order_id != original.client_order_id
    assert len(after[0].close_client_order_id) == 36


@pytest.mark.asyncio
async def test_recovery_excludes_foreign_scopes_and_empty_json_requests(pg_engine):
    from app.services.open_close_request import load_close_after_open

    journal, original = await setup(pg_engine)
    assert await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id) == []
    foreign = opening(broker_account_id='OTHER')
    other_mode = opening(mode='sandbox' if original.mode == 'live' else 'live')
    for value in (foreign, other_mode):
        assert await journal.reserve(value)
        await request_close_after_open(journal.sessions, value, CloseAfterOpen('MANUAL', value.created_at))
    assert await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id) == []
    a = (await load_close_after_open(journal.sessions, mode=foreign.mode, broker_account_id=foreign.broker_account_id))[0]
    b = (await load_close_after_open(journal.sessions, mode=other_mode.mode, broker_account_id=other_mode.broker_account_id))[0]
    assert a.close_client_order_id != b.close_client_order_id


@pytest.mark.asyncio
async def test_recovery_rejects_malformed_nonempty_request(pg_engine):
    from sqlalchemy import update
    from app.models.broker_order_intent import BrokerOrderIntent
    from app.services.open_close_request import load_close_after_open

    journal, original = await setup(pg_engine)
    async with journal.sessions() as db:
        await db.execute(update(BrokerOrderIntent).values(close_request={}))
        await db.commit()
    with pytest.raises(ValueError, match='Invalid durable'):
        await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)


@pytest.mark.asyncio
async def test_runtime_protective_close_remembers_trigger_while_open_is_pending(pg_engine):
    from tests.test_runtime_open_completion_postgres import initialized
    from app.services.open_close_request import load_close_after_open

    journal, original, rt = await initialized(pg_engine)
    from unittest.mock import AsyncMock
    rt.broker.request_cancel_order = AsyncMock(side_effect=TimeoutError('unknown'))
    rt.broker.order_state = AsyncMock(side_effect=TimeoutError('unknown'))
    result = await rt._submit_close_reconciled('FIGI', 'OFFLINE', 10, 94.0, 'STOP_LOSS')
    assert result['status'] == 'blocked_existing'
    recovered = await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)
    assert len(recovered) == 1 and recovered[0].request.reason == 'STOP_LOSS'
    assert rt.pending_orders['FIGI'].meta['close_after_open'] == recovered[0].request.to_payload()
    assert (await journal.load(original)).unresolved_figi == 'FIGI'
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_foreign_scope_cannot_save_close_trigger(pg_engine):
    from tests.test_runtime_open_completion_postgres import initialized

    journal, original, rt = await initialized(pg_engine)
    rt.broker._account = 'OTHER'
    result = await rt._submit_close_reconciled('FIGI', 'OFFLINE', 10, 94.0, 'STOP_LOSS')
    assert result['status'] == 'blocked_existing'
    assert (await journal.load(original)).close_request is None
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_timeout', [False, True])
async def test_cancel_racing_full_fill_accounts_actual_units_and_keeps_exit_pause(pg_engine, cancel_timeout):
    from unittest.mock import AsyncMock
    from tests.test_runtime_open_completion_postgres import initialized

    journal, original, rt = await initialized(pg_engine)
    rt.broker.request_cancel_order = AsyncMock(side_effect=TimeoutError('unknown') if cancel_timeout else None)
    result = await rt._submit_close_reconciled('FIGI', 'OFFLINE', 10, 94.0, 'STOP_LOSS')
    assert result['status'] == 'blocked_existing'
    rt.broker.request_cancel_order.assert_awaited_once_with(original.client_order_id,
        expected_scope=(original.mode, original.broker_account_id))
    row = await journal.load(original)
    assert row.accounted_units == row.runtime_accounted_units == 20
    assert row.unresolved_figi is None and row.close_request['reason'] == 'STOP_LOSS'
    assert rt._exit_qty['FIGI'] == 20 and rt.entries_paused
    assert ('close_after_open', original.mode, original.broker_account_id, original.client_order_id) in rt._entry_pause().blockers
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_ack_without_terminal_state_keeps_open_reservation(pg_engine):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from tests.test_runtime_open_completion_postgres import initialized
    from tests.test_runtime_close_projection_postgres import state

    journal, original, rt = await initialized(pg_engine)
    partial = state(count=1)
    partial.direction = 1
    rt.broker.order_state = AsyncMock(return_value=partial)
    rt.broker.get_position = AsyncMock(return_value=SimpleNamespace(figi='FIGI', side='LONG', qty=10, entry_price=102))
    rt.broker.request_cancel_order = AsyncMock()
    await rt._submit_close_reconciled('FIGI', 'OFFLINE', 10, 94.0, 'STOP_LOSS')
    row = await journal.load(original)
    assert row.unresolved_figi == 'FIGI' and row.accounted_units == 10
    assert 'FIGI' in rt.pending_orders and rt.entries_paused
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_restart_restores_exit_pause_after_opening_is_released(pg_engine):
    from tests.test_runtime_close_recovery_postgres import runtime

    journal, original = await setup(pg_engine)
    partial = await account(journal, original, 1, 'CANCELLED')
    await request_close_after_open(journal.sessions, original, CloseAfterOpen('STOP_LOSS', original.created_at))
    await acknowledge_open(journal.sessions, original, units=10, local_position_id=partial.local_position_id)
    await pg_engine.dispose()
    rt = runtime(pg_engine)
    assert await rt._restore_open_intents() == 0
    recovered = await rt._restore_close_after_open_requests()
    assert len(recovered) == 1 and rt.entries_paused
    assert rt.pending_orders == {}
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_mode_change_after_saving_trigger_prevents_cancellation(pg_engine, monkeypatch):
    from unittest.mock import AsyncMock
    from tests.test_runtime_open_completion_postgres import initialized
    import app.services.open_close_request as service

    journal, original, rt = await initialized(pg_engine)
    original_save = service.request_close_after_open
    async def changed(*args, **kwargs):
        saved = await original_save(*args, **kwargs)
        rt.broker_mode = 'sandbox'
        return saved
    monkeypatch.setattr(service, 'request_close_after_open', changed)
    rt.broker.request_cancel_order = AsyncMock()
    await rt._submit_close_reconciled('FIGI', 'OFFLINE', 10, 94.0, 'STOP_LOSS')
    assert (await journal.load(original)).close_request['reason'] == 'STOP_LOSS'
    rt.broker.request_cancel_order.assert_not_awaited()
    rt.broker.order_state.assert_not_awaited()
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_empty_cancelled_open_has_no_position_to_close(pg_engine):
    from app.services.open_close_request import load_close_after_open

    journal, original = await setup(pg_engine)
    await request_close_after_open(journal.sessions, original, CloseAfterOpen('STOP_LOSS', original.created_at))
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id) == []
    assert (await journal.load(original)).close_request is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['complete', 'partial', 'runtime_pending', 'wrong_entry', 'wrong_position'])
async def test_close_completion_requires_full_ack_and_original_position_link(pg_engine, case):
    from dataclasses import replace
    from decimal import Decimal
    from sqlalchemy import update
    from app.models.broker_order_intent import BrokerOrderIntent
    from app.services.broker_order_journal import CloseIntent
    from app.services.close_position_snapshot import ClosePositionSnapshot
    from app.services.open_close_request import load_close_after_open, PendingCloseAfterOpen

    journal, original = await setup(pg_engine)
    full = await account(journal, original, 2, 'FILLED')
    request = CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3))
    await request_close_after_open(journal.sessions, original, request)
    await acknowledge_open(journal.sessions, original, units=20, local_position_id=full.local_position_id)
    pending = PendingCloseAfterOpen(original, request)
    snapshot = ClosePositionSnapshot(ticker='OFFLINE', position_side='LONG', position_units=20,
        entry_price=Decimal('103'), observed_at=request.requested_at,
        local_position_id=full.local_position_id, entry_client_order_id=original.client_order_id)
    if case == 'wrong_entry': snapshot = replace(snapshot, entry_client_order_id='OTHER')
    if case == 'wrong_position': snapshot = replace(snapshot, local_position_id=full.local_position_id + 1)
    close = CloseIntent(mode=original.mode, broker_account_id=original.broker_account_id,
        client_order_id=pending.close_client_order_id, figi=original.figi, side='SELL',
        requested_units=20, lot_size=10, created_at=request.requested_at, position_snapshot=snapshot)
    assert await journal.reserve(close)
    units = 10 if case == 'partial' else 20
    async with journal.sessions() as db:
        await db.execute(update(BrokerOrderIntent).where(BrokerOrderIntent.client_order_id == close.client_order_id).values(
            status='SETTLED', unresolved_figi=None, broker_status='CANCELLED' if case == 'partial' else 'FILLED',
            observed_units=units, accounted_units=units, runtime_accounted_units=0 if case == 'runtime_pending' else units))
        await db.commit()
    if case.startswith('wrong'):
        with pytest.raises(ValueError, match='not linked'):
            await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)
    else:
        recovered = await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)
        assert len(recovered) == (0 if case == 'complete' else 1)
    assert (await journal.load(original)).close_request == request.to_payload()


@pytest.mark.asyncio
async def test_confirmed_empty_exit_clears_only_its_scoped_pause(pg_engine):
    from tests.test_runtime_close_recovery_postgres import runtime

    journal, original = await setup(pg_engine)
    await request_close_after_open(journal.sessions, original, CloseAfterOpen('STOP_LOSS', original.created_at))
    rt = runtime(pg_engine)
    await rt._restore_close_after_open_requests()
    rt.entries_paused = True
    rt._entry_pause().set_blocker('close_after_open', True, original.mode, 'OTHER', 'FOREIGN')
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert await rt._restore_close_after_open_requests() == []
    assert ('close_after_open', original.mode, original.broker_account_id, original.client_order_id) not in rt._entry_pause().blockers
    assert ('close_after_open', original.mode, 'OTHER', 'FOREIGN') in rt._entry_pause().blockers
    assert rt._entry_pause().manual and rt.entries_paused
