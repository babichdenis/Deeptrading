from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine, prepare, apply_migration, intent
from app.services.broker_order_journal import BrokerOrderJournal


@pytest.mark.asyncio
async def test_local_uuid_rejection_is_durable_and_idempotent(pg_engine):
    await prepare(pg_engine)
    async with pg_engine.begin() as conn:
        await conn.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(pg_engine, expire_on_commit=False))
    request = intent(client_order_id='paper-924087c24fcd')
    assert await journal.reserve(request)
    assert await journal.reject_sdk_invalid_id(request)
    assert not await journal.reject_sdk_invalid_id(request)
    row = await journal.load(request)
    assert row.status == 'LOCAL_REJECTED_INVALID_UUID'
    assert row.broker_status is None and row.broker_order_id is None
    assert row.unresolved_figi is None and row.observed_units == 0
    with pytest.raises(ValueError):
        await journal.reject_sdk_invalid_id(replace(request, client_order_id=str(uuid4())))


@pytest.mark.asyncio
async def test_opening_rejection_and_execution_evidence_guard(pg_engine):
    from app.services.open_order_intent import OpenIntent
    from app.models.broker_order_intent import BrokerOrderIntent
    await prepare(pg_engine)
    async with pg_engine.begin() as conn:
        await conn.run_sync(apply_migration)
    sessions = async_sessionmaker(pg_engine, expire_on_commit=False)
    journal = BrokerOrderJournal(sessions)
    base = intent()
    request = OpenIntent(base.mode, base.broker_account_id, 'paper-open-invalid', base.figi,
                         'BUY', 20, 10, base.created_at, ticker='TEST', strategy_id='rsi_reversal')
    assert await journal.reserve(request)
    async with sessions() as db:
        row = await db.get(BrokerOrderIntent, (request.mode, request.broker_account_id, request.client_order_id))
        row.broker_order_id = 'observed-broker-order'
        await db.commit()
    with pytest.raises(ValueError, match='execution evidence'):
        await journal.reject_sdk_invalid_id(request)
    row = await journal.load(request)
    assert row.unresolved_figi == request.figi
    assert row.status == 'PENDING_RECONCILIATION'
    other = replace(request, client_order_id='paper-other-invalid', figi='OTHER')
    assert await journal.reserve(other)
    assert await journal.reject_sdk_invalid_id(other)
    assert (await journal.load(other)).entry_plan['ticker'] == 'TEST'
