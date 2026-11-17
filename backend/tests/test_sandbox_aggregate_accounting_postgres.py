import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.sandbox_trade import SandboxTrade
from app.services.open_fill_accounting import settle_open, acknowledge_open
from app.services.sandbox_order_evidence import SandboxOrderAggregate
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_open_fill_accounting_postgres import setup


@pytest.mark.asyncio
async def test_aggregate_settles_once_and_acknowledges_without_fabricated_stages(pg_engine):
    journal, intent = await setup(pg_engine, mode='sandbox')
    now = intent.created_at + timedelta(minutes=1)
    proof = SandboxOrderAggregate(intent.broker_account_id, intent.client_order_id, 'EXCHANGE',
        intent.figi, intent.side, intent.lot_size, 2, 20, Decimal('1499.6'), Decimal('0.7498'), now, now, 'rub')
    assert await journal.observe(intent, broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    results = await asyncio.gather(*(settle_open(journal.sessions, intent, proof, as_of=now) for _ in range(2)))
    assert sorted(r.applied_units for r in results) == [0,20]
    assert results[0].local_position_id == results[1].local_position_id
    async with journal.sessions() as db:
        position = (await db.scalars(select(SandboxTrade))).one()
        meta = json.loads(position.meta)
        assert position.entry_price == Decimal('74.98') and position.qty == 20
        assert position.stop_loss == intent.stop_loss
        assert meta['entry_time_confirmed'] is None
        assert meta['entry_time_source'] == 'sandbox_order_timestamp'
        assert 'fills' not in meta['entry_fill_evidence']
    with pytest.raises(ValueError, match='aggregate changed'):
        await settle_open(journal.sessions, intent, replace(proof, notional=Decimal('1500')), as_of=now)
    ack = await acknowledge_open(journal.sessions, intent, units=20, local_position_id=results[0].local_position_id)
    assert ack.released
    row = await journal.load(intent)
    assert row.status == 'SETTLED' and row.runtime_accounted_units == 20 and row.unresolved_figi is None
    from types import SimpleNamespace
    from app.services.entry_commission_evidence import linked_entry_commission
    closing = SimpleNamespace(mode='sandbox', broker_account_id=intent.broker_account_id,
        figi=intent.figi, side='SELL', requested_units=10, lot_size=intent.lot_size,
        position_snapshot=SimpleNamespace(entry_client_order_id=intent.client_order_id,
            local_position_id=results[0].local_position_id))
    async with journal.sessions() as db:
        assert await linked_entry_commission(db, closing) == Decimal('0.3749')
        closing.position_snapshot.local_position_id += 1
        with pytest.raises(ValueError, match='identity mismatch'):
            await linked_entry_commission(db, closing)



@pytest.mark.asyncio
async def test_sandbox_close_aggregate_journal_is_idempotent(pg_engine):
    from tests.test_close_stage_settlement_postgres import setup as close_setup, NOW
    from app.models.paper import PaperTrade
    journal, original = await close_setup(pg_engine)
    original = replace(original, mode='sandbox', client_order_id='SANDBOX_CLOSE')
    assert await journal.reserve(original)
    proof = SandboxOrderAggregate(original.broker_account_id, original.client_order_id, 'EXCHANGE',
        original.figi, original.side,10,2,20,Decimal('2100'),Decimal('1.05'),
        NOW+timedelta(seconds=1),NOW+timedelta(seconds=2),'rub')
    assert await journal.observe(original,broker_order_id='EXCHANGE',broker_status='FILLED',executed_units=20)
    first = await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=proof)
    repeat = await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=proof)
    assert first.applied_units == 20 and repeat.applied_units == 0
    assert repeat.trade_id == first.trade_id
    async with journal.sessions() as db:
        trades=list(await db.scalars(select(PaperTrade)))
    assert len(trades)==1 and trades[0].net_pnl==Decimal('96.95')


@pytest.mark.asyncio
async def test_linked_open_close_uses_both_broker_commissions_once(pg_engine):
    from app.models.paper import PaperTrade
    from app.services.broker_order_journal import CloseIntent
    from app.services.close_position_snapshot import ClosePositionSnapshot
    journal, opening = await setup(pg_engine, mode='sandbox')
    entered = opening.created_at + timedelta(minutes=1)
    proof = SandboxOrderAggregate(opening.broker_account_id,opening.client_order_id,'ENTRY',
        opening.figi,'BUY',10,2,20,Decimal('2000'),Decimal('.75'),entered,entered,'rub')
    assert await journal.observe(opening,broker_order_id='ENTRY',broker_status='FILLED',executed_units=20)
    booked = await settle_open(journal.sessions,opening,proof,as_of=entered)
    await acknowledge_open(journal.sessions,opening,units=20,local_position_id=booked.local_position_id)
    exit_at = entered + timedelta(minutes=5)
    snapshot = ClosePositionSnapshot(ticker=opening.ticker,position_side='LONG',position_units=20,
        entry_price=Decimal('100'),observed_at=exit_at,entry_time=entered,
        entry_time_source='sandbox_order_timestamp',strategy_id=opening.strategy_id,
        exit_reason='STOP',internal_account_id=1,commission_rate=Decimal('.001'),
        cost_basis='configured_estimate',local_position_id=booked.local_position_id,
        entry_client_order_id=opening.client_order_id)
    closing = CloseIntent(mode='sandbox',broker_account_id=opening.broker_account_id,
        client_order_id='EXIT',figi=opening.figi,side='SELL',requested_units=20,lot_size=10,
        created_at=exit_at,position_snapshot=snapshot)
    assert await journal.reserve(closing)
    exit_proof = SandboxOrderAggregate(opening.broker_account_id,'EXIT','EXIT_BROKER',opening.figi,
        'SELL',10,2,20,Decimal('2100'),Decimal('1.05'),exit_at,exit_at,'rub')
    assert await journal.observe(closing,broker_order_id='EXIT_BROKER',broker_status='FILLED',executed_units=20)
    for _ in range(2):
        await journal.settle(closing,broker_order_id='EXIT_BROKER',cumulative_units=20,evidence=exit_proof)
    async with journal.sessions() as db:
        trades=list(await db.scalars(select(PaperTrade)))
    assert len(trades)==1
    assert trades[0].entry_commission==Decimal('.75')
    assert trades[0].commission==Decimal('1.80') and trades[0].net_pnl==Decimal('98.20')
