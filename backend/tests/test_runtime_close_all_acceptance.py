import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.live_broker import LiveBroker, LivePosition
from app.bot.runtime import PaperBotRuntime


def close_fixture(outcome):
    figi = 'OFFLINE_CLOSE_TEST'
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    pos = LivePosition(figi, 'TEST', 'LONG', 20, 100, now, None, None)
    runtime = PaperBotRuntime.__new__(PaperBotRuntime)
    runtime.config = SimpleNamespace(commission_rate=0.0005, slippage_bps=0, params={})
    runtime._bot_now = lambda: now
    runtime._in_trading_session = lambda: True
    runtime._log = Mock()
    runtime.events = SimpleNamespace(log=Mock())
    runtime.buffers = {}
    runtime._held = {figi}
    runtime._exit_plans = {figi: object()}
    runtime._exit_qty = {figi: 20}
    runtime._exit_side = {figi: 'LONG'}
    runtime._entry_bar_index = {figi: 1}
    runtime._bar_counter = 5
    runtime._opposite_count = {}
    runtime._last_exit_bar = {}
    runtime._peak_pnl = {}
    runtime.pending_orders = {}
    runtime._st_close = AsyncMock()
    runtime._entry_provenance = AsyncMock(return_value=(None, "unknown", None, None))
    runtime._check_circuit_breaker = AsyncMock()
    runtime._clear_exit_state = Mock(side_effect=lambda key: runtime._exit_plans.pop(key, None))
    broker = LiveBroker.__new__(LiveBroker)
    broker.positions = AsyncMock(return_value=[pos])
    broker._get_lot = AsyncMock(return_value=1)
    broker.get_position = AsyncMock(return_value=pos)

    async def close(*args, **kwargs):
        await asyncio.sleep(0)
        if outcome == 'timeout':
            raise TimeoutError('offline unknown result')
        if outcome == 'unknown':
            return None
        qty = 10 if outcome == 'partial' else 20
        return SimpleNamespace(qty=qty, exit_price=99., net_pnl=-float(qty),
                               client_order_id=kwargs.get('client_order_id'),
                               broker_order_id='OFFLINE_ORDER', id=1)

    broker.close_position = AsyncMock(side_effect=close)
    runtime.broker = broker
    return runtime, figi


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown', 'timeout', 'partial', 'full'])
async def test_close_all_reports_only_confirmed_full_closure(outcome):
    runtime, figi = close_fixture(outcome)
    result = await runtime.close_all()
    full = outcome == 'full'
    assert result['closed'] == int(full)
    assert (figi in runtime._held) == (not full)
    assert (figi in runtime._exit_plans) == (not full)
    assert runtime._clear_exit_state.call_count == int(full)
    if not full:
        runtime._st_close.assert_not_awaited()
        assert figi in runtime.pending_orders
        if outcome == 'partial':
            assert runtime._exit_qty[figi] == 10
        await runtime.close_all()
        assert runtime.broker.close_position.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_close_all_does_not_submit_twice():
    runtime, figi = close_fixture('unknown')
    results = await asyncio.gather(runtime.close_all(), runtime.close_all())
    assert runtime.broker.close_position.await_count == 1
    assert all(result['closed'] == 0 for result in results)
    assert figi in runtime._held and figi in runtime._exit_plans


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [None, float('nan'), float('inf')])
async def test_close_all_requires_confirmed_finite_exit_price(price):
    runtime, figi = close_fixture('full')

    async def invalid_receipt(*args, **kwargs):
        return SimpleNamespace(qty=20, exit_price=price, net_pnl=0.,
            client_order_id=kwargs.get('client_order_id'), broker_order_id='OFFLINE_ORDER', id=1)

    runtime.broker.close_position.side_effect = invalid_receipt
    result = await runtime.close_all()
    assert result['closed'] == 0
    assert figi in runtime._held and figi in runtime._exit_plans
    assert figi in runtime.pending_orders
    runtime._clear_exit_state.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown', 'timeout', 'partial', 'full'])
async def test_intrabar_preserves_unresolved_exposure(outcome):
    runtime, figi = close_fixture(outcome)
    runtime._trail_stop = {figi: 100.}
    runtime._exit_target = {}
    result = await runtime._intrabar_handle(figi, 99.)
    full = outcome == 'full'
    assert (result == 'closed') == full
    assert (figi in runtime._held) == (not full)
    assert (figi in runtime._exit_plans) == (not full)
    assert runtime._clear_exit_state.call_count == int(full)
    if full:
        runtime._st_close.assert_awaited_once()
    else:
        runtime._st_close.assert_not_awaited()
        assert figi in runtime.pending_orders
        if outcome == 'partial':
            assert runtime._exit_qty[figi] == 10
        await runtime._intrabar_handle(figi, 98.)
        assert runtime.broker.close_position.await_count == 1


@pytest.mark.asyncio
async def test_intrabar_and_close_all_share_submission_exclusion():
    runtime, figi = close_fixture('unknown')
    runtime._trail_stop = {figi: 100.}
    runtime._exit_target = {}
    await asyncio.gather(runtime._intrabar_handle(figi, 99.), runtime.close_all())
    assert runtime.broker.close_position.await_count == 1
    assert figi in runtime._held and figi in runtime._exit_plans
    runtime._st_close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('side,close_side', [('LONG','SELL'),('SHORT','BUY')])
async def test_submitted_close_metadata_can_be_reconciled(side, close_side, monkeypatch):
    runtime, figi = close_fixture('unknown')
    position=runtime.broker.get_position.return_value
    position.side=side
    runtime.broker._get_lot.return_value=10
    runtime.broker.mode='live'
    runtime.broker._account='OFFLINE_ACCOUNT'
    runtime.broker.sessions=object()
    stored=AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe',stored)
    runtime.broker.order_state=AsyncMock(return_value=SimpleNamespace(
        execution_report_status=3,lots_executed=0,order_id='OFFLINE_EXCHANGE'))
    await runtime.close_all()
    order=runtime.pending_orders[figi]
    assert order.side==close_side
    assert order.meta['requested_units']==20
    assert order.meta['lot_size']==10 and order.meta['requested_lots']==2
    await runtime._reconcile_pending_orders()
    runtime.broker.order_state.assert_awaited_once_with(order.id,figi,close_side,2)
    assert order.status=='CANCELLED' and figi not in runtime.pending_orders
    assert figi in runtime._held and figi in runtime._exit_plans


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preflight', 'submitted'])
async def test_account_change_cannot_apply_old_close_to_new_scope(phase):
    runtime, figi = close_fixture('full')
    runtime.broker.mode='live'
    runtime.broker._account='OFFLINE_OLD'
    if phase=='preflight':
        async def changed_lot(*args):
            runtime.broker._account='OFFLINE_NEW'
            return 1
        runtime.broker._get_lot.side_effect=changed_lot
    else:
        async def changed_close(*args,**kwargs):
            runtime.broker._account='OFFLINE_NEW'
            return SimpleNamespace(qty=20,exit_price=99.,net_pnl=-20.,
                client_order_id=kwargs['client_order_id'],broker_order_id='OFFLINE_ORDER')
        runtime.broker.close_position.side_effect=changed_close
    result=await runtime.close_all()
    assert result['closed']==0
    assert figi in runtime._held and figi in runtime._exit_plans
    runtime._st_close.assert_not_awaited()
    if phase=='preflight':
        runtime.broker.close_position.assert_not_awaited()
    else:
        assert runtime.pending_orders[figi].meta['broker_scope']==('live','OFFLINE_OLD')
        runtime.broker.order_state=AsyncMock()
        await runtime._reconcile_pending_orders()
        runtime.broker.order_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown','timeout','partial','position_unavailable'])
async def test_universe_removal_keeps_tracking_unclosed_position(outcome):
    runtime, figi = close_fixture(outcome)
    runtime.tickers={figi:'TEST'}
    runtime.strategies={figi:object()}
    runtime.stream_universe=[figi]
    runtime.feed=SimpleNamespace(figis=[figi],remove_figis=Mock())
    if outcome=='position_unavailable':
        runtime.broker.get_position.side_effect=TimeoutError('offline missing position snapshot')
    result=await runtime.deactivate_figi(figi,price=99.)
    assert result['ok'] is False and result['closed']==0 and result['pending_position']
    assert figi in runtime._held and figi in runtime._exit_plans
    assert figi in runtime.strategies and figi in runtime.stream_universe
    runtime.feed.remove_figis.assert_not_called()
    runtime._clear_exit_state.assert_not_called()
    if outcome=='partial':
        assert runtime._exit_qty[figi]==10


@pytest.mark.asyncio
async def test_universe_removal_completes_after_confirmed_close():
    runtime, figi = close_fixture('full')
    runtime.tickers={figi:'TEST'}
    runtime.strategies={figi:object()}
    runtime.stream_universe=[figi]
    runtime.universe=[{'figi':figi}]
    runtime._cand_queue={}
    runtime.feed=SimpleNamespace(figis=[figi],remove_figis=Mock())
    result=await runtime.deactivate_figi(figi,price=99.)
    assert result['ok'] and result['closed']==1
    assert figi not in runtime._held and figi not in runtime._exit_plans
    assert figi not in runtime.strategies and figi not in runtime.stream_universe
    runtime.feed.remove_figis.assert_called_once_with([figi])
    runtime._st_close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown','timeout','partial','full'])
async def test_portfolio_reduction_counts_only_full_closures(outcome):
    runtime, figi = close_fixture(outcome)
    runtime.tcs_to_bbg={}
    result=await runtime.reduce_positions(1.0,'offline_reduce')
    full=outcome=='full'
    assert result['closed']==int(full)
    assert bool(result['unresolved'])==(not full)
    assert (figi in runtime._held)==(not full)
    assert (figi in runtime._exit_plans)==(not full)
    if full:
        runtime._st_close.assert_awaited_once()
    else:
        runtime._st_close.assert_not_awaited()
        if outcome=='partial':
            assert runtime._exit_qty[figi]==10
        await runtime.reduce_positions(1.0,'offline_reduce')
        assert runtime.broker.close_position.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown','timeout','partial','full'])
async def test_signal_close_uses_same_request_and_preserves_unresolved(outcome):
    from app.bot.runtime_models import BotOrder
    runtime,figi=close_fixture(outcome)
    runtime._trace=None
    order=BotOrder(id='OFFLINE_SIGNAL',figi=figi,ticker='TEST',action='close',
        side='SELL',qty=1,status='SUBMITTED')
    runtime.pending_orders[figi]=order
    bar=SimpleNamespace(open=99.,ts=runtime._bot_now())
    result=await runtime._execute_pending(figi,bar)
    assert result==(outcome=='full')
    assert runtime.broker.close_position.call_args.kwargs['client_order_id']==order.id
    assert runtime.broker.close_position.call_args.kwargs['expected_units']==20
    assert (figi in runtime._held)==(outcome!='full')
    if outcome!='full':
        assert runtime.pending_orders[figi] is order
        runtime._st_close.assert_not_awaited()
        await runtime._execute_pending(figi,bar)
        assert runtime.broker.close_position.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['overnight_force_close','weekend_force_close'])
@pytest.mark.parametrize('outcome',['unknown','timeout','partial','full'])
async def test_session_exit_preserves_unconfirmed_position(outcome,reason):
    runtime,figi=close_fixture(outcome)
    result=await runtime._close_session_position(figi,101.,reason)
    assert result==(outcome=='full')
    assert (figi in runtime._held)==(outcome!='full')
    assert (figi in runtime._exit_plans)==(outcome!='full')
    if outcome=='full':
        runtime._st_close.assert_awaited_once()
        assert runtime._st_close.call_args.args[1]==99.
    else:
        runtime._st_close.assert_not_awaited()
        if outcome=='partial':assert runtime._exit_qty[figi]==10
        await runtime._close_session_position(figi,101.,reason)
        assert runtime.broker.close_position.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('caller',['close_all','intrabar','reduce','signal','session','universe'])
async def test_already_projected_close_never_invokes_legacy_accounting(caller):
    from app.bot.runtime_models import BotOrder
    runtime,figi=close_fixture('full')
    runtime.tickers={figi:'TEST'}
    runtime.tcs_to_bbg={}
    runtime._trail_stop={figi:100.}
    runtime._exit_target={}
    runtime._trace=None
    order=BotOrder(id='PROJECTED',figi=figi,ticker='TEST',action='close',side='SELL',qty=20,status='SUBMITTED')
    async def projected(*args,**kwargs):
        runtime._held.discard(figi)
        runtime._exit_plans.pop(figi,None)
        runtime.pending_orders.pop(figi,None)
        order.status='FILLED'
        return dict(status='reconciled_closed',trade=None,order=order,residual=0)
    runtime._submit_close_reconciled=AsyncMock(side_effect=projected)
    if caller=='close_all':assert (await runtime.close_all())['closed']==1
    elif caller=='intrabar':assert await runtime._intrabar_handle(figi,99.)=='closed'
    elif caller=='reduce':assert (await runtime.reduce_positions(1.,'OFFLINE'))['closed']==1
    elif caller=='signal':
        runtime.pending_orders[figi]=order
        assert await runtime._execute_pending(figi,SimpleNamespace(open=99.,ts=runtime._bot_now()))
    elif caller=='session':assert await runtime._close_session_position(figi,99.,'OFFLINE')
    else:
        runtime.strategies={figi:object()}
        runtime.stream_universe=[figi]
        runtime.universe=[{'figi':figi}]
        runtime._cand_queue={}
        runtime.feed=SimpleNamespace(figis=[figi],remove_figis=Mock())
        result=await runtime.deactivate_figi(figi,price=99.)
        assert result['ok'] and result['closed']==1
    runtime._st_close.assert_not_awaited()
    runtime._submit_close_reconciled.assert_awaited_once()
