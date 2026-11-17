import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.sandbox_trade import SandboxTrade
from app.services.broker_fill_evidence import ConfirmedFill, OrderFillEvidence
from app.services.open_fill_accounting import settle_open
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_open_order_journal_postgres import opening_journal, opening


async def setup(engine, **changes):
    journal = await opening_journal(engine)
    async with engine.begin() as db:
        await db.run_sync(SandboxTrade.__table__.drop)
        await db.run_sync(SandboxTrade.__table__.create)
    original = opening(**changes)
    assert await journal.reserve(original)
    return journal, original


def evidence(original, count):
    fills = tuple(ConfirmedFill(f'STAGE{i}', 10, Decimal(100 + i * 2), original.created_at + timedelta(minutes=i))
                  for i in range(1, count + 1))
    return OrderFillEvidence(original.mode, original.broker_account_id, original.client_order_id,
        'EXCHANGE', original.figi, original.side, 'rub', 2, count, 10, fills)


async def account(journal, original, count, status):
    proof = evidence(original, count)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status=status, executed_units=count * 10)
    return await settle_open(journal.sessions, original, proof, as_of=original.created_at + timedelta(hours=1))


@pytest.mark.asyncio
async def test_partial_then_full_updates_same_position_once(pg_engine):
    journal, original = await setup(pg_engine)
    partial = await account(journal, original, 1, 'PARTIAL_FILLED')
    await pg_engine.dispose()
    full = await account(journal, original, 2, 'FILLED')
    repeat = await account(journal, original, 2, 'FILLED')
    assert partial.local_position_id == full.local_position_id == repeat.local_position_id
    assert (partial.applied_units, full.applied_units, repeat.applied_units) == (10, 10, 0)
    async with journal.sessions() as db:
        position = (await db.scalars(select(SandboxTrade))).one()
        assert position.qty == 20 and position.entry_price == 103
        assert position.entry_time == original.created_at + timedelta(minutes=1)
        assert position.stop_loss == original.stop_loss and position.take_profit == original.take_profit
        assert len(json.loads(position.meta)['entry_fill_evidence']['fills']) == 2
        assert json.loads(position.meta)['entry_plan']['exit_policy'] == original.exit_policy.to_payload()
    row = await journal.load(original)
    assert row.accounted_units == 20 and row.runtime_accounted_units == 0 and row.unresolved_figi == original.figi


@pytest.mark.asyncio
async def test_concurrent_accounting_produces_one_position(pg_engine):
    journal, original = await setup(pg_engine)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    results = await asyncio.gather(*(settle_open(journal.sessions, original, evidence(original, 2),
        as_of=original.created_at + timedelta(hours=1)) for _ in range(4)))
    assert sorted(result.applied_units for result in results) == [0, 0, 0, 20]
    assert len({result.local_position_id for result in results}) == 1


@pytest.mark.asyncio
async def test_failure_after_insert_rolls_back_position_and_checkpoint(pg_engine):
    journal, original = await setup(pg_engine)
    def fail(*args): raise RuntimeError('injected insert failure')
    event.listen(SandboxTrade, 'after_insert', fail)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            await account(journal, original, 2, 'FILLED')
    finally:
        event.remove(SandboxTrade, 'after_insert', fail)
    async with journal.sessions() as db:
        assert list(await db.scalars(select(SandboxTrade))) == []
    row = await journal.load(original)
    assert row.accounted_units == 0 and row.entry_accounting is None
    assert (await account(journal, original, 2, 'FILLED')).applied_units == 20


@pytest.mark.asyncio
async def test_changed_old_stage_cannot_rewrite_previous_position(pg_engine):
    journal, original = await setup(pg_engine)
    await account(journal, original, 1, 'PARTIAL_FILLED')
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    proof = evidence(original, 2)
    proof = replace(proof, fills=(replace(proof.fills[0], price=Decimal(99)), proof.fills[1]))
    with pytest.raises(ValueError, match='stages changed'):
        await settle_open(journal.sessions, original, proof, as_of=original.created_at + timedelta(hours=1))
    assert (await journal.load(original)).accounted_units == 10


@pytest.mark.asyncio
async def test_partial_cancel_records_only_executed_units(pg_engine):
    journal, original = await setup(pg_engine)
    result = await account(journal, original, 1, 'CANCELLED')
    assert result.accounted_units == 10
    assert (await journal.load(original)).unresolved_figi == original.figi


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('entry_price', Decimal(999)), ('qty', 9), ('broker_account_id', 'OTHER')])
async def test_local_position_drift_is_not_overwritten(pg_engine, field, value):
    journal, original = await setup(pg_engine)
    first = await account(journal, original, 1, 'PARTIAL_FILLED')
    async with journal.sessions() as db:
        position = await db.get(SandboxTrade, first.local_position_id)
        setattr(position, field, value)
        await db.commit()
    with pytest.raises(ValueError, match='local opening position changed'):
        await account(journal, original, 2, 'FILLED')
    assert (await journal.load(original)).accounted_units == 10


@pytest.mark.asyncio
async def test_foreign_evidence_cannot_create_local_position(pg_engine):
    journal, original = await setup(pg_engine)
    assert await journal.observe(original, broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    proof = replace(evidence(original, 2), broker_account_id='FOREIGN')
    with pytest.raises(ValueError, match='does not match'):
        await settle_open(journal.sessions, original, proof, as_of=original.created_at + timedelta(hours=1))
    assert (await journal.load(original)).accounted_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('side,initial_stop,target,tightened', [
    ('BUY', '95', '110', '101'), ('SELL', '110', '95', '104')])
async def test_late_fill_preserves_tightened_trailing_and_existing_metadata(pg_engine, side, initial_stop, target, tightened):
    journal, original = await setup(pg_engine, side=side, stop_loss=Decimal(initial_stop), take_profit=Decimal(target))
    first = await account(journal, original, 1, 'PARTIAL_FILLED')
    async with journal.sessions() as db:
        position = await db.get(SandboxTrade, first.local_position_id)
        position.stop_loss = Decimal(tightened)
        position.take_profit = None
        position.trailing_active = True
        position.meta = json.dumps({**json.loads(position.meta), 'protection_revision': 'retained'})
        await db.commit()
    full = await account(journal, original, 2, 'FILLED')
    assert full.local_position_id == first.local_position_id and full.applied_units == 10
    async with journal.sessions() as db:
        position = await db.get(SandboxTrade, first.local_position_id)
        assert position.qty == 20 and position.entry_price == 103
        assert position.stop_loss == Decimal(tightened) and position.take_profit is None and position.trailing_active
        assert json.loads(position.meta)['protection_revision'] == 'retained'
        assert len(json.loads(position.meta)['entry_fill_evidence']['fills']) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['looser_stop', 'missing_stop', 'unmarked_change', 'changed_target', 'changed_plan'])
async def test_late_fill_rejects_unproven_protection_change(pg_engine, case):
    journal, original = await setup(pg_engine)
    first = await account(journal, original, 1, 'PARTIAL_FILLED')
    async with journal.sessions() as db:
        position = await db.get(SandboxTrade, first.local_position_id)
        position.stop_loss = Decimal('101')
        position.take_profit = None
        position.trailing_active = True
        if case == 'looser_stop': position.stop_loss = Decimal('90')
        elif case == 'missing_stop': position.stop_loss = None
        elif case == 'unmarked_change': position.trailing_active = False
        elif case == 'changed_target': position.take_profit = Decimal('999')
        else: position.meta = json.dumps({**json.loads(position.meta), 'entry_plan': {}})
        await db.commit()
    with pytest.raises(ValueError, match='local opening position changed'):
        await account(journal, original, 2, 'FILLED')
    assert (await journal.load(original)).accounted_units == 10
