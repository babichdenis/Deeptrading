from dataclasses import replace

import pytest
from sqlalchemy import update

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from app.services.broker_order_journal import CloseIntent
from app.services.close_position_snapshot import ClosePositionSnapshot
from app.services.open_close_request import load_close_after_open, prepare_close_after_open
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_close_after_open_handoff_postgres import ready


async def close_row(journal, original, proof, *, executed, status, runtime_ack=True):
    snapshot = ClosePositionSnapshot(ticker='OFFLINE', position_side='LONG', position_units=proof.units,
        entry_price=proof.entry_price, observed_at=proof.entry_time,
        local_position_id=proof.local_position_id, entry_client_order_id=original.client_order_id)
    intent = CloseIntent(mode=original.mode, broker_account_id=original.broker_account_id,
        client_order_id=proof.close_client_order_id, figi=original.figi, side='SELL',
        requested_units=proof.units, lot_size=10, created_at=proof.entry_time, position_snapshot=snapshot)
    assert await journal.reserve(intent)
    async with journal.sessions() as db:
        await db.execute(update(BrokerOrderIntent).where(BrokerOrderIntent.client_order_id == intent.client_order_id).values(
            status='SETTLED' if runtime_ack else 'ACCOUNTED_PENDING_RUNTIME',
            unresolved_figi=None if runtime_ack else original.figi, broker_status=status,
            observed_units=executed, accounted_units=executed, runtime_accounted_units=executed if runtime_ack else 0))
        if executed and runtime_ack:
            position = await db.get(SandboxTrade, proof.local_position_id)
            if executed < proof.units: position.qty -= executed
            else: position.exit_time = proof.entry_time
        await db.commit()


@pytest.mark.asyncio
async def test_cancelled_partial_chain_uses_new_stable_id_and_only_remaining_quantity(pg_engine):
    journal, original, rt = await ready(pg_engine)
    pending = (await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id))[0]
    first = await prepare_close_after_open(journal.sessions, pending)
    await close_row(journal, original, first, executed=10, status='CANCELLED')
    await pg_engine.dispose()
    second = await prepare_close_after_open(journal.sessions, pending)
    assert second.units == 10 and first.units == 20
    assert second.close_client_order_id != first.close_client_order_id
    assert second.close_client_order_id == (await prepare_close_after_open(journal.sessions, pending)).close_client_order_id
    assert second.local_position_id == first.local_position_id and second.entry_price == first.entry_price
    await close_row(journal, original, second, executed=10, status='FILLED')
    assert await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id) == []
    assert await prepare_close_after_open(journal.sessions, pending) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('executed,status,ack', [(0, 'CANCELLED', True), (10, 'CANCELLED', False), (10, 'PARTIAL_FILLED', False)])
async def test_no_progress_or_unacknowledged_close_never_authorizes_another_request(pg_engine, executed, status, ack):
    journal, original, rt = await ready(pg_engine)
    pending = (await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id))[0]
    first = await prepare_close_after_open(journal.sessions, pending)
    await close_row(journal, original, first, executed=executed, status=status, runtime_ack=ack)
    assert await prepare_close_after_open(journal.sessions, pending) is None
    assert len(await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id)) == 1
