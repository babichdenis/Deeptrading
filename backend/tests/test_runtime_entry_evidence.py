import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from t_tech.invest.schemas import MoneyValue, OrderDirection, OrderExecutionReportStatus, OrderStage, OrderState

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime
from app.models.sandbox_trade import SandboxTrade


NOW = datetime(2026,11,16,12,tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['confirmed','timeout','inconsistent_status','mismatched_quantity','scope_changed','scope_changed_during_commit'])
async def test_actual_entry_persistence_keeps_execution_provenance(monkeypatch,case):
    engine=create_engine('sqlite://')
    SandboxTrade.__table__.create(engine)
    session=Session(engine)
    class AsyncSession:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def execute(self,query):return session.execute(query)
        async def flush(self):session.flush()
        async def commit(self):
            session.commit()
            if case=='scope_changed_during_commit':broker._account='OTHER' 
        def add(self,row):
            row.id=1
            session.add(row)
    monkeypatch.setattr('app.bot.runtime.SessionLocal',AsyncSession)
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    broker=LiveBroker.__new__(LiveBroker)
    broker.mode='live'
    broker._account='OFFLINE_ACCOUNT'
    state=OrderState(order_id='EXCHANGE',order_request_id='REQUEST',figi='FIGI',
        direction=OrderDirection.ORDER_DIRECTION_BUY,lots_requested=3,lots_executed=2,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
        currency='rub',order_date=NOW-timedelta(minutes=3),
        stages=[OrderStage(price=MoneyValue(currency='rub',units=100,nano=0),quantity=2,
                           trade_id='TRADE',execution_time=NOW-timedelta(minutes=2))])
    async def lookup(*args):
        if case=='timeout':raise TimeoutError('OFFLINE')
        if case=='inconsistent_status':
            state.execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW
        if case=='scope_changed':broker._account='OTHER'
        if case=='mismatched_quantity':
            state.lots_executed=3
            state.execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
            state.stages[0].quantity=3
        return state
    broker.order_state=AsyncMock(side_effect=lookup)
    rt.broker=broker
    rt.broker_mode='live'
    rt.config=SimpleNamespace(test_name='',leverage=1)
    rt._regimes={}
    rt._entry_regime={}
    rt.strategies={}
    rt.pending_orders={}
    rt.events=SimpleNamespace(log=lambda *a,**k:None)
    rt._entry_card=lambda figi,meta,regime:dict(meta)
    rt._bot_now=lambda:NOW
    order=SimpleNamespace(id='REQUEST',ticker='TEST',side='BUY',status='APPROVED',
                          meta={'lot_size':10,'requested_lots':3})
    try:
        result=await rt._persist_entry_fill('FIGI',order,2,100,SimpleNamespace(stop_loss=99,take_profit=102))
        rows=session.scalars(select(SandboxTrade)).all()
        if case=='scope_changed':
            assert not result and rows==[] and rt.entries_paused
            assert rt.pending_orders['FIGI'] is order
            return
        if case=='scope_changed_during_commit':
            assert not result and len(rows)==1 and rows[0].broker_account_id=='OFFLINE_ACCOUNT'
            assert rt.pending_orders['FIGI'] is order and rt.entries_paused
            return
        assert result and len(rows)==1
        row=rows[0]
        meta=json.loads(row.meta)
        assert row.qty==20 and row.broker_account_id=='OFFLINE_ACCOUNT'
        assert meta['entry_observed_at']==NOW.isoformat()
        if case=='confirmed':
            assert meta['entry_time_source']=='broker_history'
            assert meta['entry_time_confirmed']==(NOW-timedelta(minutes=2)).isoformat()
            assert row.entry_time.replace(tzinfo=timezone.utc)==NOW-timedelta(minutes=2)
            assert meta['entry_fill_evidence']['fills'][0]['trade_id']=='TRADE'
            assert meta['entry_fill_evidence']['fills'][0]['units']==20
            state.lots_executed=3
            state.execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
            state.stages[0].quantity=3
            assert await rt._persist_entry_fill('FIGI',order,3,100,SimpleNamespace(stop_loss=99,take_profit=102))
            session.refresh(row)
            updated=json.loads(row.meta)
            assert row.qty==30 and updated['entry_fill_evidence']['executed_lots']==3
            assert updated['entry_time_confirmed']==meta['entry_time_confirmed']
            broker.order_state=AsyncMock(side_effect=TimeoutError('OFFLINE'))
            assert await rt._persist_entry_fill('FIGI',order,3,100,SimpleNamespace(stop_loss=99,take_profit=102))
            session.refresh(row)
            assert json.loads(row.meta)['entry_time_source']=='broker_history'
            assert json.loads(row.meta)['entry_fill_evidence']['executed_lots']==3
            assert await rt._persist_entry_fill('FIGI',order,3,101,SimpleNamespace(stop_loss=99,take_profit=102))
            session.refresh(row)
            assert json.loads(row.meta)['entry_time_source']=='unknown'
            assert json.loads(row.meta)['entry_fill_evidence'] is None
        else:
            assert meta['entry_time_source']=='unknown'
            assert meta['entry_time_confirmed'] is None and meta['entry_fill_evidence'] is None
    finally:
        session.close()
        engine.dispose()
