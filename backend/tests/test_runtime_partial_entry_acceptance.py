from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize('lot_size', [1, 10, 1000])
@pytest.mark.parametrize('executed_lots', [1, 3, 0, 7, -1])
@pytest.mark.parametrize('ledger_failure', [False, True])
@pytest.mark.parametrize('universe_removed', [False, True])
async def test_partial_entry_retains_intent_and_never_resubmits(monkeypatch, lot_size, executed_lots, ledger_failure, universe_removed, risk_case=None, submission_case=None):
    monkeypatch.delenv('ENTRY_CONFIRM_ENGINE', raising=False)
    monkeypatch.delenv('ENTRY_FILTER_D1', raising=False)
    submitted = []
    broker = LiveBroker.__new__(LiveBroker)
    broker._account = 'OFFLINE_TEST_ACCOUNT'
    broker.mode = 'live'
    broker.order_state = AsyncMock(side_effect=TimeoutError('offline history unavailable'))
    broker._log = lambda *args: None
    broker.sessions = object()
    broker._get_lot = AsyncMock(return_value=lot_size)
    broker.flush_portfolio = AsyncMock()
    broker.get_position = AsyncMock(return_value=None)
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.reserve',
                        AsyncMock(side_effect=RuntimeError('offline reservation failure') if ledger_failure else None,
                                  return_value=True))
    monkeypatch.setattr('app.services.broker_order_journal.BrokerOrderJournal.observe', AsyncMock(return_value=True))

    def post(**request):
        submitted.append(request)
        if submission_case == 'connection':
            raise ConnectionError('response lost after broker acceptance')
        if submission_case == 'account':
            broker._account = 'SECOND'
        if submission_case == 'runtime_broker':
            rt.broker = SimpleNamespace()
        return SimpleNamespace(
            order_id="EXCHANGE", order_request_id=request["order_id"], figi=request["instrument_id"],
            direction=request["direction"], executed_order_price=SimpleNamespace(units=100, nano=0),
            lots_requested=3, lots_executed=executed_lots, execution_report_status=1 if executed_lots == 3 else 4 if executed_lots == 0 else 5)

    broker._api_post_order = post

    class InstrumentSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query):
            return SimpleNamespace(scalar_one_or_none=lambda: 999)

    monkeypatch.setattr('app.bot.runtime.SessionLocal', InstrumentSession)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker = broker
    rt._replay_cur = None
    rt._replay_from = None
    rt._candidate_mode = 'live'
    rt._candidate_figis = {'FIGI123'}
    rt._candidate_observed_at = datetime.now(timezone.utc)
    rt._candidate_catalog = {'FIGI123': dict(
        figi='FIGI123', ticker='TEST', uid='offline', class_code='TQBR', lot=lot_size,
        api_trade_available_flag=True, buy_available_flag=True,
        sell_available_flag=True, short_enabled_flag=False)}
    from app.bot.risk import RiskSnapshot
    rt._ensure_risk_fresh = AsyncMock(return_value=True)
    rt.risk_snapshot = lambda: RiskSnapshot(0, 1000, rt.entries_paused)
    rt.entries_paused = False
    rt.pending_orders = {}
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    rt._log_persist_queue = deque()
    rt._trace = None
    for name in (
        '_approvals_since', '_ai_reject_until', '_ai_reject_reason', '_exit_plans',
        '_exit_side', '_exit_entry_px', '_entry_bar_index', '_held_since',
        '_exit_qty', '_pos_leverage', '_trail_active', '_trail_stop', '_peak_pnl',
        '_gb_peak', '_trail_info', '_exit_target',
    ):
        setattr(rt, name, {})
    rt._held = set()
    rt._clear_exit_state = lambda *args, **kwargs: None
    rt._trace_order_reject = lambda *args, **kwargs: None
    rt._st_open = AsyncMock(side_effect=RuntimeError("offline ledger failure") if ledger_failure else None)
    rt._bar_counter = 0
    rt.config = SimpleNamespace(
        mode='live', use_ensemble=False, initial_sl_atr=4.0, atr_risk_reward=4.0,
        commission_rate=0.003, slippage_bps=2.0, sl_mode='fixed', leverage=1.0,
        stop_pct=0.02, target_pct=0.04, strategy_id='offline')
    order = SimpleNamespace(
        id='offline-partial', action='open', status='APPROVED', ticker='TEST',
        side='BUY', qty=3, meta={'lot_size': lot_size}, to_dict=lambda: {})
    rt.pending_orders['FIGI123'] = order
    candle = SimpleNamespace(open=100.0, ts=datetime.now(timezone.utc))

    if universe_removed:
        rt._candidate_figis.clear()
        assert await rt._execute_pending('FIGI123', candle) is False
        assert order.status == 'REJECTED'
        assert submitted == []
        rt._st_open.assert_not_awaited()
        return
    if risk_case is not None:
        rt.broker_mode = 'live'
        rt.config.daily_loss_limit = 100
        del rt._ensure_risk_fresh
        del rt.risk_snapshot
        rt._daily_pnl_cache = None
        if risk_case == 'unavailable':
            rt.refresh_daily_pnl = AsyncMock(side_effect=RuntimeError('offline risk unavailable'))
        else:
            async def loss():
                rt._daily_pnl_cache = (rt._bot_now(), -101)
                rt._daily_pnl_cache_scope = rt._daily_pnl_scope()
                return -101
            rt.refresh_daily_pnl = AsyncMock(side_effect=loss)
        assert await rt._execute_pending('FIGI123', candle) is False
        assert order.status == 'CANCELLED'
        assert submitted == []
        rt._st_open.assert_not_awaited()
        return
    if submission_case is not None:
        assert await rt._execute_pending('FIGI123', candle) is False
        assert order.status == 'PENDING_RECONCILIATION'
        assert order.meta['broker_scope'] == ('live', 'OFFLINE_TEST_ACCOUNT')
        assert order.meta['requested_lots'] == 3
        assert rt.pending_orders['FIGI123'] is order
        rt._st_open.assert_not_awaited()
        assert len(submitted) == 1
        if submission_case != 'runtime_broker':
            assert await rt._execute_pending('FIGI123', candle) is False
            assert len(submitted) == 1
        if submission_case == 'account':
            await rt._reconcile_pending_orders()
            broker.order_state.assert_not_awaited()
        return
    assert await rt._execute_pending('FIGI123', candle) is False
    rt._st_open.assert_not_awaited()
    assert 'FIGI123' not in rt._exit_qty
    if ledger_failure:
        assert order.status == 'REJECTED' and submitted == []
        return
    assert len(submitted) == 1 and order.status == 'PENDING_RECONCILIATION'
    assert order.meta['durable_open'] and rt.entries_paused
    assert rt.pending_orders['FIGI123'] is order
    assert await rt._execute_pending('FIGI123', candle) is False
    assert len(submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('lot', [None, 0, -1, True, 1.5, '10'])
async def test_missing_confirmed_lot_rejects_before_submission(lot):
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker = LiveBroker.__new__(LiveBroker)
    rt.broker.open_position = AsyncMock()
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    order = SimpleNamespace(id='offline', action='open', status='APPROVED',
                            ticker='TEST', meta={'lot_size': lot})
    rt.pending_orders = {'FIGI': order}
    assert await rt._execute_pending('FIGI', SimpleNamespace()) is False
    assert order.status == 'REJECTED'
    rt.broker.open_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('risk_case', ['unavailable', 'loss'])
async def test_queued_entry_checks_actual_risk_before_post(monkeypatch, risk_case):
    await test_partial_entry_retains_intent_and_never_resubmits(
        monkeypatch, 10, 3, False, False, risk_case=risk_case)


@pytest.mark.asyncio
@pytest.mark.parametrize('submission_case', ['connection', 'account', 'runtime_broker'])
async def test_entry_submission_uncertainty_never_becomes_rejection_or_second_post(monkeypatch, submission_case):
    await test_partial_entry_retains_intent_and_never_resubmits(
        monkeypatch, 10, 3, False, False, submission_case=submission_case)
