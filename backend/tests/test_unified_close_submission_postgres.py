import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, text, update

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.paper import PaperTrade
from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_close_projection_postgres import initialized, state
from tests.test_live_close_postgres_acceptance import adapter
from tests.test_close_stage_settlement_postgres import NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,rate',[('full',0.0005),('partial',0.0005),('timeout',0.0005),('full',0)])
async def test_actual_submission_and_late_fill_share_runtime_accounting(pg_engine,isolated_postgres,monkeypatch,outcome,rate):
    journal,original,rt=await initialized(pg_engine,monkeypatch)
    class SubmissionClock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW
    monkeypatch.setattr('app.bot.live_broker.datetime',SubmissionClock)
    monkeypatch.setattr('app.bot.runtime._new_order_id',lambda:'REQUEST_1')
    entry_time=original.position_snapshot.entry_time
    meta=dict(client_order_id='ENTRY_1',lot_size=10,entry_time_source='broker_history',
        entry_time_confirmed=entry_time.isoformat(),entry_fill_evidence=dict(version=1,
            client_order_id='ENTRY_1',broker_order_id='ENTRY_EXCHANGE',figi='FIGI',side='BUY',currency='rub',
            requested_lots=2,executed_lots=2,lot_size=10,
            fills=[dict(trade_id='ENTRY_STAGE',units=20,price='100',executed_at=entry_time.isoformat())]))
    async with pg_engine.begin() as db:
        await db.execute(text('DELETE FROM broker_order_intents'))
        await db.execute(text("UPDATE paper_accounts SET name='default' WHERE id=1"))
        await db.execute(update(SandboxTrade).where(SandboxTrade.id==7).values(mode='sandbox',meta=json.dumps(meta)))
    broker,calls=await adapter(pg_engine,isolated_postgres,outcome=outcome,prepare_schema=False)
    broker.config.commission_rate=rate
    held=await broker.get_position('FIGI')
    remaining=SimpleNamespace(figi='FIGI',ticker='OFFLINE',side='LONG',qty=10,entry_price=100)
    async def position(figi):
        return held if not calls else remaining if outcome=='partial' else None
    broker.get_position=AsyncMock(side_effect=position)
    broker.positions=AsyncMock(return_value=[held])
    broker.flush_portfolio=AsyncMock()
    broker.order_state=AsyncMock(return_value=state(1) if outcome=='partial' else state())
    rt.broker=broker
    rt.broker_mode='sandbox'
    rt.pending_orders={}
    rt._bot_now=lambda:NOW.replace(hour=12) if calls else NOW
    rt.config=SimpleNamespace(test_name='OFFLINE',daily_loss_limit=1000)
    rt.buffers={}
    rt._in_trading_session=lambda:True
    rt._st_close=AsyncMock()
    rt._peak_pnl={}
    monkeypatch.setattr('app.bot.runtime.SessionLocal',broker.sessions)
    result=await rt.close_all()
    assert len(calls)==1
    rt._st_close.assert_not_awaited()
    if outcome=='full':
        assert result['closed']==1 and result['pending']==[]
        assert not rt.pending_orders and 'FIGI' not in rt._held
    else:
        assert result['closed']==0 and 'FIGI' in rt.pending_orders
    async with broker.sessions() as db:
        trades=(await db.scalars(select(PaperTrade))).all()
        ledger=await db.get(BrokerOrderIntent,('sandbox','OFFLINE_ACCOUNT','REQUEST_1'))
        assert ledger.position_snapshot['local_position_id']==7
        assert ledger.position_snapshot['entry_client_order_id']=='ENTRY_1'
        assert len(trades)==int(outcome!='timeout')
        if outcome=='full':
            assert float(trades[0].exit_price)==105
            assert float(trades[0].commission)==4100*rate
        if outcome=='partial':assert trades[0].qty==10 and float(trades[0].exit_price)==102
    if outcome!='full':
        broker.get_position=AsyncMock(return_value=None)
        broker.order_state.return_value=state()
        await rt._reconcile_pending_orders()
        assert not rt.pending_orders and 'FIGI' not in rt._held
        async with broker.sessions() as db:
            trades=(await db.scalars(select(PaperTrade).order_by(PaperTrade.id))).all()
            assert sum(trade.qty for trade in trades)==20
            assert [float(trade.exit_price) for trade in trades]==([102,108] if outcome=='partial' else [105])
    assert len(calls)==1
    rt._st_close.assert_not_awaited()
