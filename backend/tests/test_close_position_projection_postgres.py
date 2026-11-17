import json
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from app.services.close_position_projection import project_close_fills
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_close_stage_settlement_postgres import setup, observe, evidence


async def linked_position(engine):
    journal,original=await setup(engine,local_position_id=7,entry_client_order_id='ENTRY_1')
    async with engine.begin() as db:
        await db.execute(text('DROP TABLE sandbox_trades'))
        await db.run_sync(SandboxTrade.__table__.create)
    snapshot=original.position_snapshot
    async with journal.sessions() as db:
        db.add(SandboxTrade(id=7,mode=original.mode,broker_account_id=original.broker_account_id,
            figi=original.figi,ticker=snapshot.ticker,side='BUY',qty=20,
            entry_time=snapshot.entry_time,entry_price=snapshot.entry_price,
            stop_loss=95,take_profit=110,entry_reason='ENTRY',test_name='OFFLINE',
            meta=json.dumps(dict(client_order_id='ENTRY_1',entry_time_source='broker_history',
                                 entry_time_confirmed=snapshot.entry_time.isoformat()))))
        await db.commit()
    return journal,original


async def settle(journal,original,count,status):
    await observe(journal,original,count,status)
    return await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=count*10,
        evidence=evidence(original,count,cancelled=status=='CANCELLED'),defer_runtime=True)


@pytest.mark.asyncio
async def test_partial_projection_retains_protection_then_full_closes_original_once(pg_engine):
    journal,original=await linked_position(pg_engine)
    await settle(journal,original,1,'PARTIAL_FILLED')
    first=await journal.apply_runtime(original,cumulative_units=10,apply=project_close_fills)
    assert first.applied_units==10 and not first.released
    async with journal.sessions() as db:
        source=await db.get(SandboxTrade,7)
        assert source.qty==10 and source.exit_time is None
        assert source.stop_loss==95 and source.take_profit==110
        closed=(await db.scalars(select(SandboxTrade).where(SandboxTrade.exit_time.is_not(None)))).one()
        assert closed.qty==10 and closed.exit_price==102
        assert json.loads(closed.exit_meta)['original_local_position_id']==7
    await pg_engine.dispose()
    await settle(journal,original,2,'FILLED')
    final=await journal.apply_runtime(original,cumulative_units=20,apply=project_close_fills)
    assert final.applied_units==10 and final.released
    repeat=await journal.apply_runtime(original,cumulative_units=20,apply=project_close_fills)
    assert repeat.applied_units==0
    async with journal.sessions() as db:
        rows=(await db.scalars(select(SandboxTrade))).all()
        assert len(rows)==2 and sum(row.qty for row in rows)==20
        assert all(row.exit_time is not None for row in rows)
        assert sum(row.net_pnl for row in rows)==Decimal('95.9')
        assert len({json.loads(row.exit_meta)['paper_trade_id'] for row in rows})==2
        assert all(json.loads(row.exit_meta)['cost_basis']=='configured_estimate' for row in rows)


@pytest.mark.asyncio
async def test_multiple_unapplied_deltas_are_projected_in_one_transaction(pg_engine):
    journal,original=await linked_position(pg_engine)
    await settle(journal,original,1,'PARTIAL_FILLED')
    await settle(journal,original,2,'FILLED')
    result=await journal.apply_runtime(original,cumulative_units=20,apply=project_close_fills)
    assert result.applied_units==20 and result.released
    async with journal.sessions() as db:
        rows=(await db.scalars(select(SandboxTrade))).all()
        assert sorted(float(row.exit_price) for row in rows)==[102,108]
        assert all(row.qty==10 for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['account','mode','request','side','quantity','price','closed','missing'])
async def test_projection_never_uses_another_position_cycle(pg_engine,change):
    journal,original=await linked_position(pg_engine)
    async with journal.sessions() as db:
        row=await db.get(SandboxTrade,7)
        if change=='account':row.broker_account_id='OTHER'
        if change=='mode':row.mode='sandbox'
        if change=='request':row.meta=json.dumps(dict(client_order_id='OTHER'))
        if change=='side':row.side='SELL'
        if change=='quantity':row.qty=10
        if change=='price':row.entry_price=101
        if change=='closed':row.exit_time=row.entry_time
        if change=='missing':await db.delete(row)
        await db.commit()
    await settle(journal,original,2,'FILLED')
    with pytest.raises(ValueError):
        await journal.apply_runtime(original,cumulative_units=20,apply=project_close_fills)
    async with journal.sessions() as db:
        ledger=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert ledger.runtime_accounted_units==0 and ledger.unresolved_figi=='FIGI'
        rows=(await db.scalars(select(SandboxTrade))).all()
        assert len(rows)==(0 if change=='missing' else 1)
        assert all(row.exit_price is None for row in rows)


@pytest.mark.asyncio
async def test_failure_after_projection_flush_rolls_back_rows_and_checkpoint(pg_engine):
    journal,original=await linked_position(pg_engine)
    await settle(journal,original,1,'PARTIAL_FILLED')
    async def interrupted(db,saved,trades):
        await project_close_fills(db,saved,trades)
        raise RuntimeError('OFFLINE interruption after projection flush')
    with pytest.raises(RuntimeError,match='interruption'):
        await journal.apply_runtime(original,cumulative_units=10,apply=interrupted)
    async with journal.sessions() as db:
        rows=(await db.scalars(select(SandboxTrade))).all()
        assert len(rows)==1 and rows[0].qty==20 and rows[0].exit_time is None
        ledger=await db.get(BrokerOrderIntent,('live','OFFLINE_ACCOUNT','REQUEST_1'))
        assert ledger.runtime_accounted_units==0
    assert (await journal.apply_runtime(original,cumulative_units=10,apply=project_close_fills)).applied_units==10
