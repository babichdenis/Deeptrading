from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot import runtime as runtime_module
from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
async def test_failed_second_broker_read_preserves_local_position(monkeypatch):
    row = SimpleNamespace(figi='OFFLINE', entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          exit_time=None, exit_price=None, exit_reason=None, net_pnl=None)
    commit = AsyncMock()

    class MemorySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

        async def commit(self):
            await commit()

    monkeypatch.setattr(runtime_module, 'SessionLocal', MemorySession)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode = 'live'
    rt.broker = SimpleNamespace(_account='OFFLINE_ACCOUNT', flush_portfolio=AsyncMock(),
        positions=AsyncMock(return_value=[]),
        get_position=AsyncMock(side_effect=TimeoutError('offline unavailable')))
    rt._log = Mock()
    await rt._reconcile_positions()
    assert row.exit_time is None
    assert row.exit_price is None
    assert row.net_pnl is None
    commit.assert_not_awaited()
    assert any('UNKNOWN' in str(call) for call in rt._log.call_args_list)
    assert not any('✓' in str(call) for call in rt._log.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'partial', 'cancelled', 'future', 'duplicate'])
async def test_orphan_close_requires_complete_operation_and_never_invents_net(monkeypatch, case):
    from datetime import timedelta
    from t_tech.invest import OperationState
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(figi='OFFLINE', entry_time=now-timedelta(days=1),
                          exit_time=None, exit_price=None, exit_reason=None, net_pnl=None,
                          entry_price=100, qty=10, side='BUY', commission=None)
    op = SimpleNamespace(figi='OFFLINE', date=now-timedelta(hours=1),
        type='Продажа', quantity=10, quantity_rest=0,
        state=OperationState.OPERATION_STATE_EXECUTED,
        price=SimpleNamespace(units=110, nano=0))
    if case == 'partial': op.quantity = 4
    if case == 'cancelled': op.state = OperationState.OPERATION_STATE_CANCELED
    if case == 'future': op.date = now+timedelta(days=1)
    operations = [op, op] if case == 'duplicate' else [op]
    commit = AsyncMock()

    class MemorySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
        async def commit(self): await commit()

    monkeypatch.setattr(runtime_module, 'SessionLocal', MemorySession)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode = 'live'
    rt.broker = SimpleNamespace(_account='OFFLINE_ACCOUNT', flush_portfolio=AsyncMock(),
        positions=AsyncMock(return_value=[]), get_position=AsyncMock(return_value=None),
        operations=AsyncMock(return_value=SimpleNamespace(operations=operations)))
    rt._log = Mock()
    await rt._reconcile_positions()
    assert row.net_pnl is None
    assert row.commission is None
    if case == 'valid':
        assert row.exit_price == 110
        assert row.exit_time == op.date
        commit.assert_awaited_once()
    else:
        assert row.exit_time is None
        commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['orphan','rebuild','duplicate','inflight'])
async def test_pending_execution_excludes_ticker_from_position_repairs(monkeypatch,case):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(figi='OFFLINE',entry_time=now-timedelta(days=1),
                          exit_time=None,exit_price=None,exit_reason=None,net_pnl=None,
                          entry_price=100,qty=10,side='BUY',commission=None)
    duplicate = SimpleNamespace(**vars(row))
    rows = [] if case=='rebuild' else [row,duplicate] if case=='duplicate' else [row]
    position = SimpleNamespace(figi='OFFLINE',qty=10,entry_price=100,side='LONG')
    commit = AsyncMock()
    add = Mock()
    class MemorySession:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def execute(self,query):
            return SimpleNamespace(scalars=lambda:SimpleNamespace(all=lambda:rows),
                                   scalar_one_or_none=lambda:None)
        async def commit(self): await commit()
        def add(self,value): add(value)
    monkeypatch.setattr(runtime_module,'SessionLocal',MemorySession)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode='live'
    rt.broker=SimpleNamespace(_account='OFFLINE_ACCOUNT',flush_portfolio=AsyncMock(),
        positions=AsyncMock(return_value=[position] if case in ('rebuild','duplicate') else []),
        get_position=AsyncMock(return_value=None),
        operations=AsyncMock(return_value=SimpleNamespace(operations=[])))
    rt.pending_orders={} if case=='inflight' else {'OFFLINE':SimpleNamespace(action='close',status='PENDING_RECONCILIATION')}
    rt._closing_inflight={'OFFLINE'} if case=='inflight' else set()
    rt.tickers={}
    rt.config=SimpleNamespace(test_name='OFFLINE')
    rt._log=Mock()
    await rt._reconcile_positions()
    assert row.exit_time is None and row.exit_price is None
    assert duplicate.exit_time is None
    add.assert_not_called()
    commit.assert_not_awaited()
    rt.broker.get_position.assert_not_awaited()
    rt.broker.operations.assert_not_awaited()
    assert not any('✓' in str(call) for call in rt._log.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_pending',[False,True])
async def test_pending_ticker_does_not_stop_other_tickers_and_late_order_rolls_back(monkeypatch,late_pending):
    from datetime import timedelta
    from t_tech.invest import OperationState
    now=datetime.now(timezone.utc)
    def row(figi):
        return SimpleNamespace(figi=figi,entry_time=now-timedelta(days=1),exit_time=None,
            exit_price=None,exit_reason=None,net_pnl=None,entry_price=100,qty=10,side='BUY',commission=None)
    first,second=row('FIRST'),row('SECOND')
    commit,rollback=AsyncMock(),AsyncMock()
    class MemorySession:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def execute(self,query):
            return SimpleNamespace(scalars=lambda:SimpleNamespace(all=lambda:[first,second]))
        async def commit(self): await commit()
        async def rollback(self): await rollback()
    monkeypatch.setattr(runtime_module,'SessionLocal',MemorySession)
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode='live'
    pending=SimpleNamespace(action='close',status='PENDING_RECONCILIATION')
    rt.pending_orders={} if late_pending else {'FIRST':pending}
    rt._log=Mock()
    calls=0
    async def operations(*args):
        nonlocal calls
        calls+=1
        if late_pending and calls==2:
            rt.pending_orders['FIRST']=pending
        return SimpleNamespace(operations=[SimpleNamespace(figi=figi,date=now-timedelta(hours=1),
            type='Продажа',quantity=10,quantity_rest=0,state=OperationState.OPERATION_STATE_EXECUTED,
            price=SimpleNamespace(units=110,nano=0)) for figi in ('FIRST','SECOND')])
    rt.broker=SimpleNamespace(_account='OFFLINE_ACCOUNT',flush_portfolio=AsyncMock(),
        positions=AsyncMock(return_value=[]),get_position=AsyncMock(return_value=None),
        operations=AsyncMock(side_effect=operations))
    await rt._reconcile_positions()
    if late_pending:
        rollback.assert_awaited_once()
        commit.assert_not_awaited()
        assert calls==2
    else:
        assert first.exit_time is None and second.exit_price==110
        commit.assert_awaited_once()
        rollback.assert_not_awaited()
        rt.broker.get_position.assert_awaited_once_with('SECOND')
    assert not any('✓' in str(call) for call in rt._log.call_args_list)
