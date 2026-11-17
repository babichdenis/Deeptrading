from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.broker_order_intent import BrokerOrderIntent
from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent
from app.services.open_order_intent import OpenIntent
from app.services.exit_policy_snapshot import ExitPolicySnapshot
from app.engine.exits import AtrStopPolicy
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine, prepare, apply_migration, intent


async def opening_journal(engine):
    await prepare(engine)
    async with engine.begin() as db:
        await db.run_sync(apply_migration)
    return BrokerOrderJournal(async_sessionmaker(engine, expire_on_commit=False))


def opening(**changes):
    original = intent()
    result = OpenIntent(mode=original.mode, broker_account_id=original.broker_account_id,
        client_order_id=original.client_order_id, figi=original.figi, side='BUY',
        requested_units=20, lot_size=10, created_at=original.created_at,
        ticker='OFFLINE', strategy_id='OFFLINE', stop_loss=Decimal('95.123456'), take_profit=Decimal('110'),
        exit_policy=ExitPolicySnapshot.capture(AtrStopPolicy(period=21, multiplier=4, risk_reward=3)))
    return replace(result, **changes)


@pytest.mark.asyncio
async def test_open_commit_recovery_and_shared_instrument_exclusion(pg_engine):
    journal = await opening_journal(pg_engine)
    original = opening()
    assert await journal.reserve(original)
    assert not await journal.reserve(original)
    assert not await journal.reserve(intent(client_order_id='CLOSE_SAME_FIGI'))
    await pg_engine.dispose()
    rows = await journal.unresolved(mode=original.mode, broker_account_id=original.broker_account_id, action='open')
    assert len(rows) == 1
    assert OpenIntent.from_record(rows[0]) == original
    assert await journal.unresolved(mode=original.mode, broker_account_id=original.broker_account_id) == []
    with pytest.raises(ValueError, match='cannot be restored'):
        CloseIntent.from_record(rows[0])


@pytest.mark.asyncio
async def test_close_apis_cannot_account_or_observe_open_intent(pg_engine):
    journal = await opening_journal(pg_engine)
    original = opening()
    assert await journal.reserve(original)
    fake_close = intent(side='BUY')
    assert not await journal.observe(fake_close, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    with pytest.raises(ValueError): await journal.load(fake_close)
    with pytest.raises(ValueError, match='entry accounting'):
        await journal.settle(original, broker_order_id='EXCHANGE', cumulative_units=20)
    with pytest.raises(ValueError, match='entry accounting'):
        await journal.apply_runtime(original, cumulative_units=20, apply=None)
    assert (await journal.load(original)).unresolved_figi == original.figi


@pytest.mark.asyncio
async def test_open_partial_cancel_keeps_reservation_until_accounting(pg_engine):
    journal = await opening_journal(pg_engine)
    original = opening()
    assert await journal.reserve(original)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='PARTIAL_FILLED', executed_units=10)
    assert not await journal.observe(original, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=10)
    row = await journal.load(original)
    assert row.observed_units == 10 and row.accounted_units == 0 and row.unresolved_figi == 'FIGI'


@pytest.mark.asyncio
async def test_empty_cancel_releases_figi_but_never_reuses_request(pg_engine):
    journal = await opening_journal(pg_engine)
    original = opening()
    assert await journal.reserve(original)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert not await journal.reserve(original)
    assert await journal.reserve(opening(client_order_id='NEXT'))


@pytest.mark.asyncio
async def test_concurrent_open_and_close_share_one_reservation(pg_engine):
    import asyncio
    journal = await opening_journal(pg_engine)
    candidates = [opening(client_order_id='OPEN'), intent(client_order_id='CLOSE')]
    assert sum(await asyncio.gather(*(journal.reserve(item) for item in candidates))) == 1
