import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from t_tech.invest.schemas import MoneyValue, OrderStage, OrderState

from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperTrade
from app.services.broker_fill_evidence import parse_order_state_fills
from app.services.broker_order_journal import BrokerOrderJournal
from app.services.close_position_snapshot import ClosePositionSnapshot
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine, prepare, apply_migration, intent


NOW = datetime(2026,11,16,11,tzinfo=timezone.utc)


async def setup(engine, **snapshot_changes):
    await prepare(engine)
    async with engine.begin() as db:
        await db.run_sync(apply_migration)
    snapshot = ClosePositionSnapshot('OFFLINE','LONG',20,Decimal('100'),NOW,
        NOW-timedelta(hours=1),'broker_history','OFFLINE','OFFLINE',1,
        Decimal('0.001'),'configured_estimate')
    snapshot = replace(snapshot, **snapshot_changes)
    original = intent(created_at=NOW,position_snapshot=snapshot,
                      side='SELL' if snapshot.position_side=='LONG' else 'BUY')
    journal = BrokerOrderJournal(async_sessionmaker(engine,expire_on_commit=False))
    assert await journal.reserve(original)
    return journal, original


def evidence(original, count=2, cancelled=False):
    state = OrderState(order_id='EXCHANGE',order_request_id=original.client_order_id,
        figi=original.figi,direction=2 if original.side=='SELL' else 1,lots_requested=2,lots_executed=count,
        execution_report_status=3 if cancelled else 1 if count==2 else 5,
        currency='rub',order_date=NOW,stages=[
            OrderStage(trade_id=f'STAGE_{index}',quantity=1,
                price=MoneyValue(currency='rub',units=price,nano=0),
                execution_time=NOW+timedelta(minutes=index))
            for index,price in enumerate((102,108),start=1)][:count])
    return parse_order_state_fills(state,mode=original.mode,broker_account_id=original.broker_account_id,
        client_order_id=original.client_order_id,figi=original.figi,side=original.side,
        requested_lots=2,lot_size=10,as_of=NOW+timedelta(hours=1))


async def observe(journal, original, count, status):
    assert await journal.observe(original,broker_order_id='EXCHANGE',
                                 broker_status=status,executed_units=count*10)


@pytest.mark.asyncio
async def test_settlement_uses_durable_timestamp_for_close_snapshot(pg_engine):
    journal, original = await setup(pg_engine)
    await observe(journal, original, 2, 'FILLED')
    queued = replace(original, created_at=NOW - timedelta(microseconds=1), position_snapshot=None)
    result = await journal.settle(queued, broker_order_id='EXCHANGE', cumulative_units=20,
                                  evidence=evidence(original))
    assert result.applied_units == 20 and result.released


@pytest.mark.asyncio
async def test_late_partial_then_full_uses_new_stage_price_and_survives_reconnect(pg_engine):
    journal, original = await setup(pg_engine)
    await observe(journal,original,1,'PARTIAL_FILLED')
    first = await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,
                                evidence=evidence(original,1))
    assert first.applied_units==10 and not first.released
    await pg_engine.dispose()
    await observe(journal,original,2,'FILLED')
    second = await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                                 evidence=evidence(original))
    assert second.applied_units==10 and second.remaining_units==0 and second.released
    repeat = await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                                 evidence=evidence(original))
    assert repeat.applied_units==0 and repeat.trade_id==second.trade_id
    async with journal.sessions() as db:
        trades=(await db.scalars(select(PaperTrade).order_by(PaperTrade.id))).all()
        fills=(await db.scalars(select(BrokerOrderFill).order_by(BrokerOrderFill.cumulative_units))).all()
    assert [t.qty for t in trades]==[10,10]
    assert [t.exit_price for t in trades]==[Decimal('102'),Decimal('108')]
    assert [t.exit_time for t in trades]==[NOW+timedelta(minutes=1),NOW+timedelta(minutes=2)]
    assert sum(t.net_pnl for t in trades)==Decimal('95.9')
    assert [f.execution_evidence['fills'][0]['trade_id'] for f in fills]==['STAGE_1','STAGE_2']
    assert all(f.execution_evidence['cost_basis']=='configured_estimate' for f in fills)


@pytest.mark.asyncio
async def test_concurrent_late_full_settlement_is_recorded_once(pg_engine):
    journal,original=await setup(pg_engine)
    await observe(journal,original,2,'FILLED')
    results=await asyncio.gather(*[
        journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=evidence(original))
        for _ in range(8)])
    assert sum(r.applied_units for r in results)==20
    assert len({r.trade_id for r in results})==1
    async with journal.sessions() as db:
        assert len((await db.scalars(select(PaperTrade))).all())==1
        assert len((await db.scalars(select(BrokerOrderFill))).all())==1


@pytest.mark.asyncio
async def test_cancel_after_partial_releases_only_after_exact_stage_accounting(pg_engine):
    journal,original=await setup(pg_engine)
    await observe(journal,original,1,'PARTIAL_FILLED')
    first=await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,evidence=evidence(original,1))
    await observe(journal,original,1,'CANCELLED')
    final=await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,
                               evidence=evidence(original,1,cancelled=True))
    assert final.applied_units==0 and final.trade_id==first.trade_id
    assert final.released and final.remaining_units==10


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['price','identity','time'])
async def test_changed_previously_accounted_stage_cannot_create_another_trade(pg_engine,change):
    journal,original=await setup(pg_engine)
    await observe(journal,original,1,'PARTIAL_FILLED')
    await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,evidence=evidence(original,1))
    await observe(journal,original,2,'FILLED')
    full=evidence(original)
    stage=full.fills[0]
    changed=replace(stage,**({'price':Decimal('103')} if change=='price' else
        {'trade_id':'OTHER'} if change=='identity' else {'executed_at':NOW+timedelta(seconds=30)}))
    with pytest.raises(ValueError,match='changed or disappeared'):
        await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,
                             evidence=replace(full,fills=(changed,full.fills[1])))
    async with journal.sessions() as db:
        row=await db.get(BrokerOrderIntent,(original.mode,original.broker_account_id,original.client_order_id))
        assert row.accounted_units==10 and row.unresolved_figi==original.figi
        assert len((await db.scalars(select(PaperTrade))).all())==1


@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[
    {'entry_time':None,'entry_time_source':'unknown'},
    {'cost_basis':'unknown','commission_rate':None},
])
async def test_unknown_context_cannot_be_filled_with_estimates_or_current_time(pg_engine,changes):
    journal,original=await setup(pg_engine,**changes)
    await observe(journal,original,2,'FILLED')
    with pytest.raises(ValueError):
        await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=evidence(original))
    async with journal.sessions() as db:
        row=await db.get(BrokerOrderIntent,(original.mode,original.broker_account_id,original.client_order_id))
        assert row.accounted_units==0 and row.observed_units==20
        assert not (await db.scalars(select(PaperTrade))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize('side,rate,expected',[
    ('LONG','0','100'),('SHORT','0.001','-104.1'),
])
async def test_direction_and_saved_zero_rate_are_preserved(pg_engine,side,rate,expected):
    journal,original=await setup(pg_engine,position_side=side,commission_rate=Decimal(rate))
    await observe(journal,original,2,'FILLED')
    result=await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=evidence(original))
    async with journal.sessions() as db:
        trade=await db.get(PaperTrade,result.trade_id)
        assert trade.side==side and trade.net_pnl==Decimal(expected)


@pytest.mark.asyncio
async def test_legacy_accounted_part_without_stage_identity_cannot_be_silently_reconstructed(pg_engine):
    from app.services.close_fill_accounting import build_close_delta
    journal,original=await setup(pg_engine)
    await observe(journal,original,1,'PARTIAL_FILLED')
    trade,_=build_close_delta(original,evidence(original,1),0,[])
    await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=10,trade=trade)
    await observe(journal,original,2,'FILLED')
    with pytest.raises(ValueError,match='no matching stage evidence'):
        await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,evidence=evidence(original))
    async with journal.sessions() as db:
        row=await db.get(BrokerOrderIntent,(original.mode,original.broker_account_id,original.client_order_id))
        assert row.accounted_units==10
        assert len((await db.scalars(select(PaperTrade))).all())==1
