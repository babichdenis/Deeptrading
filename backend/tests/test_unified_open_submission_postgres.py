import asyncio
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from sqlalchemy import delete, select, text
from t_tech.invest.schemas import MoneyValue, PostOrderResponse

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_open_completion_postgres import initialized
from tests.test_runtime_close_projection_postgres import state
from tests.test_close_stage_settlement_postgres import NOW


async def configured(engine, cluster, monkeypatch, outcome):
    journal, original, rt = await initialized(engine)
    async with journal.sessions() as db:
        await db.execute(delete(BrokerOrderIntent))
        await db.commit()
    rt._entry_pause().blockers.clear()
    original = replace(original, client_order_id='90af1178-c0c2-4662-9bcd-d1f2e5c2de46')
    order = rt.pending_orders['FIGI']
    order.id = original.client_order_id
    order.status = 'APPROVED'
    order.meta = {'lot_size': 10}
    rt.config = SimpleNamespace(mode='live', use_ensemble=False, daily_loss_limit=1000,
        initial_sl_atr=4, atr_risk_reward=4, commission_rate=0.003, slippage_bps=2,
        sl_mode='fixed', leverage=1, stop_pct=0.02, target_pct=0.04, strategy_id='OFFLINE')
    for name in ('_approvals_since', '_ai_reject_until', '_ai_reject_reason'):
        setattr(rt, name, {})
    rt._candidate_mode = 'live'
    rt._candidate_figis = {'FIGI'}
    rt._candidate_observed_at = NOW + timedelta(hours=1)
    rt._candidate_catalog = {'FIGI': dict(figi='FIGI', ticker='OFFLINE', uid='offline', class_code='TQBR', lot=10,
        api_trade_available_flag=True, buy_available_flag=True, sell_available_flag=True, short_enabled_flag=False)}
    rt._log_persist_queue = deque()
    rt._trace = None
    rt._trace_order_reject = lambda *args, **kwargs: None
    rt._st_open = AsyncMock(side_effect=AssertionError('legacy entry persistence forbidden'))
    rt.broker._get_lot = AsyncMock(return_value=10)
    rt.broker.get_position.side_effect = lambda figi: None if not calls else SimpleNamespace(
        figi='FIGI', side='LONG', qty=10 if outcome == 'partial' else 20, entry_price=102 if outcome == 'partial' else 105)
    report = state(count=1 if outcome == 'partial' else 2)
    report.order_request_id = original.client_order_id
    report.direction = 1
    rt.broker.order_state = AsyncMock(return_value=report)
    if outcome == 'timeout': rt.broker.order_state.side_effect = TimeoutError('history unavailable')
    class BrokerClock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    class RuntimeClock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW + timedelta(hours=1)
    monkeypatch.setattr('app.bot.live_broker.datetime', BrokerClock)
    monkeypatch.setattr('app.bot.runtime.datetime', RuntimeClock)
    monkeypatch.delenv('ENTRY_CONFIRM_ENGINE', raising=False)
    monkeypatch.delenv('ENTRY_FILTER_D1', raising=False)
    async with engine.connect() as db: schema = await db.scalar(text('SELECT current_schema()'))
    calls = []
    async def committed(request):
        connection = await asyncpg.connect(user='journal_test', host='127.0.0.1', port=cluster.port,
                                          database='postgres', server_settings={'search_path': schema})
        try:
            row = await connection.fetchrow('SELECT action,requested_units,entry_plan FROM broker_order_intents WHERE client_order_id=$1', request['order_id'])
            assert row is not None and row['action'] == 'open' and row['requested_units'] == 20 and row['entry_plan']
        finally: await connection.close()
    def post(**request):
        asyncio.run(committed(request))
        calls.append(request)
        if outcome == 'timeout': raise TimeoutError('response lost after acceptance')
        return PostOrderResponse(order_id='EXCHANGE', order_request_id=request['order_id'], figi='FIGI', direction=1,
            lots_requested=2, lots_executed=1 if outcome == 'partial' else 2,
            execution_report_status=5 if outcome == 'partial' else 1,
            executed_order_price=MoneyValue(currency='rub', units=99, nano=0))
    rt.broker._api_post_order = post
    return journal, original, rt, order, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['full', 'partial', 'timeout'])
async def test_runtime_adapter_reservation_stages_inventory_single_path(pg_engine, isolated_postgres, monkeypatch, outcome):
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, outcome)
    candle = SimpleNamespace(open=100, ts=NOW)
    result = await rt._execute_pending('FIGI', candle)
    assert len(calls) == 1
    assert result is (outcome == 'full')
    rt._st_open.assert_not_awaited()
    async with journal.sessions() as db:
        row = await db.get(BrokerOrderIntent, ('live', 'OFFLINE_ACCOUNT', '90af1178-c0c2-4662-9bcd-d1f2e5c2de46'))
        positions = list(await db.scalars(select(SandboxTrade)))
    if outcome == 'full':
        assert len(positions) == 1 and positions[0].entry_price == 105 and positions[0].qty == 20
        assert row.unresolved_figi is None and row.runtime_accounted_units == 20
        assert not rt.pending_orders and not rt.entries_paused
    else:
        assert rt.pending_orders['FIGI'] is order and rt.entries_paused
        assert not await rt._execute_pending('FIGI', candle)
        assert len(calls) == 1
        assert row.unresolved_figi == 'FIGI'
        assert len(positions) == (1 if outcome == 'partial' else 0)
        if positions: assert positions[0].qty == 10 and positions[0].entry_price == 102


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['storage', 'held', 'lot', 'observation'])
async def test_open_preflight_and_observation_failures_cannot_duplicate_post(pg_engine, isolated_postgres, monkeypatch, case):
    from app.services.broker_order_journal import BrokerOrderJournal
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'full')
    if case == 'storage':
        monkeypatch.setattr(BrokerOrderJournal, 'reserve', AsyncMock(side_effect=RuntimeError('storage down')))
    elif case == 'held':
        rt.broker.get_position.side_effect = None
        rt.broker.get_position.return_value = SimpleNamespace(figi='FIGI', side='LONG', qty=10, entry_price=100)
    elif case == 'lot': rt.broker._get_lot.return_value = 20
    else: monkeypatch.setattr(BrokerOrderJournal, 'observe', AsyncMock(side_effect=RuntimeError('observation unavailable')))
    assert not await rt._execute_pending('FIGI', SimpleNamespace(open=100, ts=NOW))
    assert len(calls) == (1 if case == 'observation' else 0)
    assert order.status == ('PENDING_RECONCILIATION' if case == 'observation' else 'REJECTED')
    rt._st_open.assert_not_awaited()
    async with journal.sessions() as db:
        assert list(await db.scalars(select(SandboxTrade))) == []


@pytest.mark.asyncio
async def test_timeout_recovers_original_request_in_new_runtime_without_post(pg_engine, isolated_postgres, monkeypatch):
    from app.bot.live_broker import EntrySubmissionUnknown
    from tests.test_runtime_close_recovery_postgres import runtime
    from app.services.exit_policy_snapshot import ExitPolicySnapshot
    from app.engine.exits import FixedSlTpPolicy
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'timeout')
    assert not await rt._execute_pending('FIGI', SimpleNamespace(open=100, ts=NOW))
    rt.broker.get_position.side_effect = None
    rt.broker.get_position.return_value = None
    with pytest.raises(EntrySubmissionUnknown):
        await rt.broker.open_position('FIGI', 'OFFLINE', 'BUY', 2, 100, 98, 104, 'OFFLINE',
            client_order_id='90af1178-c0c2-4662-9bcd-d1f2e5c2de46', plan_meta=dict(lot_size=10, exit_policy=ExitPolicySnapshot.capture(FixedSlTpPolicy()).to_payload()))
    assert len(calls) == 1
    await pg_engine.dispose()
    restarted = runtime(pg_engine)
    assert await restarted._restore_open_intents() == 1
    assert restarted.pending_orders['FIGI'].id == '90af1178-c0c2-4662-9bcd-d1f2e5c2de46'
    assert not await restarted._execute_pending('FIGI', SimpleNamespace())
    restarted.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_task_cancellation_during_post_keeps_original_pending_and_recovers(pg_engine, isolated_postgres, monkeypatch):
    import threading
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'full')
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_post = rt.broker._api_post_order
    def delayed(**request):
        response = original_post(**request)
        entered.set()
        try:
            if not release.wait(5): raise RuntimeError('test release missing')
            return response
        finally: finished.set()
    rt.broker._api_post_order = delayed
    task = asyncio.create_task(rt._execute_pending('FIGI', SimpleNamespace(open=100, ts=NOW)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert rt.pending_orders['FIGI'] is order and order.status == 'PENDING_RECONCILIATION'
        assert order.meta['durable_open'] and rt.entries_paused
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    await rt._reconcile_pending_orders()
    assert not rt.pending_orders and not rt.entries_paused
    assert len(calls) == 1 and rt._exit_qty['FIGI'] == 20
    rt._st_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_rsi_strategy_does_not_require_ensemble_parameters(pg_engine, isolated_postgres, monkeypatch):
    from app.engine.wave1 import RsiReversalStrategy
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'full')
    rt.strategies = {'FIGI': RsiReversalStrategy()}
    assert await rt._execute_pending('FIGI', SimpleNamespace(open=100, ts=NOW))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_single_rsi_atr_entry_persists_protection(pg_engine, isolated_postgres, monkeypatch):
    from app.bot.runtime_models import BotConfig
    from app.engine.models import Candle
    from app.engine.wave1 import RsiReversalStrategy
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'full')
    defaults = BotConfig()
    for name in ('atr_period', 'trail_activation_comm_mult', 'trail_distance_atr',
                 'trail_compress_r', 'trail_min_factor', 'trail_min_atr', 'trail_vol_boost'):
        setattr(rt.config, name, getattr(defaults, name))
    rt.config.sl_mode = 'atr'
    rt.strategies = {'FIGI': RsiReversalStrategy()}
    rt.buffers = {'FIGI': deque([Candle(ts=NOW-timedelta(minutes=5*(30-i)),
        open=100, high=101, low=99, close=100, volume=100) for i in range(30)])}
    from app.bot.execution_reference import ExecutionReference
    assert await rt._execute_pending('FIGI', ExecutionReference('FIGI', 100, NOW, NOW))
    row = await journal.load(original)
    assert row.entry_plan['exit_policy']['policy_id'] == 'atr_stop'
    assert row.entry_plan['exit_policy']['parameters']['multiplier'] == 4
    assert row.entry_plan['exit_policy']['parameters']['risk_reward'] == 4
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dispatch_to_durable_journal_once_without_another_bar(pg_engine, isolated_postgres, monkeypatch):
    from app.bot.execution_reference import ExecutionReference
    from app.bot.pending_dispatch import dispatch_pending
    journal, original, rt, order, calls = await configured(pg_engine, isolated_postgres, monkeypatch, 'full')
    rt.running = True
    rt._bot_now = lambda: NOW + timedelta(hours=1)
    order.created_at = NOW
    rt.broker.execution_references = AsyncMock(return_value={'FIGI': ExecutionReference('FIGI', 100, NOW + timedelta(hours=1), NOW + timedelta(hours=1))})
    await asyncio.gather(dispatch_pending(rt), dispatch_pending(rt))
    assert len(calls) == 1
    assert not rt.pending_orders, order.meta
    row = await journal.load(original)
    assert row.runtime_accounted_units == 20 and row.unresolved_figi is None
    async with journal.sessions() as db:
        positions = list(await db.scalars(select(SandboxTrade)))
    assert len(positions) == 1 and positions[0].entry_price == 105
