from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def runtime(monkeypatch):
    from app.bot import gates
    from app.bot.runtime import PaperBotRuntime

    monkeypatch.setattr(gates, 'run_gate_chain', lambda *_: SimpleNamespace(passed=True))
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._trace = None
    rt.pending_orders = {}
    rt.orders = []
    rt.config = SimpleNamespace(qty_per_trade=1, sessions=['day'], feed='replay',
        ensemble_capital=100, use_margin=False, margin_sizing='divide',
        ai_approval=False, queue_enabled=False)
    rt.entries_paused = False
    rt._held = set()
    rt._exit_side = {}
    rt.tickers = {'TEST': 'TEST'}
    rt.universe = [{'figi': 'TEST', 'ticker': 'TEST', 'lot': 1}]
    rt.buffers = {'TEST': [SimpleNamespace(close=100)]}
    rt.broker = SimpleNamespace(open_position=AsyncMock(), close_position=AsyncMock())
    rt.events = SimpleNamespace(log=Mock())
    rt._log = Mock()
    rt._log_no_trade = Mock()
    rt._trace_order_reject = Mock()
    rt._bot_now = lambda: datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    rt._loss_streak_block = lambda _: (False, '')
    rt.risk_snapshot = lambda: SimpleNamespace(entries_allowed=lambda: True, state='ok', daily_pnl=0)
    rt.sector_meta = AsyncMock(return_value={"TEST": {"dlong": 1.0, "dshort": 1.0}})
    rt.market_regime = AsyncMock(return_value={'state': 'neutral'})
    rt.portfolio_snapshot = AsyncMock(return_value={'equity': 10000, 'margin_liquid': 10000, 'margin_committed': 0})
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['snapshot', 'regime', 'check_order'])
async def test_portfolio_failure_never_enqueues_entry(runtime, monkeypatch, failure):
    from app.bot import portfolio

    if failure == 'snapshot':
        runtime.portfolio_snapshot.side_effect = RuntimeError('snapshot unavailable')
    elif failure == 'regime':
        runtime.market_regime.side_effect = RuntimeError('regime unavailable')
    else:
        monkeypatch.setattr(portfolio, 'check_order', Mock(side_effect=RuntimeError('invalid risk data')))
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime.orders == []
    runtime.broker.open_position.assert_not_awaited()
    assert any(call.kwargs.get('reason') == 'PORTFOLIO_CHECK_ERROR'
               for call in runtime.events.log.call_args_list)


@pytest.mark.asyncio
async def test_healthy_portfolio_reaches_pending_order(runtime):
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    runtime.portfolio_snapshot.assert_awaited_once()
    assert runtime.pending_orders['TEST'].action == 'open'
    assert len(runtime.orders) == 1


@pytest.mark.asyncio
async def test_close_not_blocked_by_unavailable_entry_portfolio(runtime):
    runtime.portfolio_snapshot.side_effect = RuntimeError('snapshot unavailable')
    await runtime._submit_order('TEST', 'TEST', 'close', 'SELL')
    runtime.portfolio_snapshot.assert_not_awaited()
    assert runtime.pending_orders['TEST'].action == 'close'
    assert len(runtime.orders) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('lot_key', ['lot', 'lot_size'])
async def test_sizing_and_portfolio_share_confirmed_lot(runtime, monkeypatch, lot_key):
    from app.bot import portfolio

    runtime.universe = [{'figi': 'TEST', lot_key: 10}]
    runtime.config.ensemble_capital = 2000
    check = Mock(return_value=(True, ''))
    monkeypatch.setattr(portfolio, 'check_order', check)
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY',
                                {'momentum': True, 'lot_size': 999, 'requested_lots': 999})
    assert runtime.pending_orders['TEST'].meta['lot_size'] == 10
    assert runtime.pending_orders['TEST'].meta['requested_lots'] == 2
    assert runtime.pending_orders['TEST'].qty == 2
    assert check.call_args.args[2] == 2000


@pytest.mark.asyncio
async def test_unknown_lot_cannot_fall_back_to_ten(runtime, monkeypatch):
    from app.bot import runtime as runtime_module

    runtime.universe = []
    runtime.config.ensemble_capital = 10000
    monkeypatch.setattr(runtime_module, 'SessionLocal', Mock(side_effect=RuntimeError('offline DB unavailable')))
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    runtime.portfolio_snapshot.assert_not_awaited()
    assert runtime._trace_order_reject.call_args.args[4] == 'sizing_price_lot'


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [float('nan'), float('inf'), 0.0, -1.0])
async def test_invalid_entry_price_never_reaches_portfolio(runtime, price):
    runtime.buffers['TEST'][-1].close = price
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    runtime.portfolio_snapshot.assert_not_awaited()


def use_offline_live_broker(runtime):
    from app.bot.live_broker import LiveBroker

    broker = LiveBroker.__new__(LiveBroker)
    broker.equity = AsyncMock(return_value=1000.0)
    broker.free_funds = AsyncMock(return_value=1000.0)
    broker.cash = AsyncMock(return_value=1000.0)
    broker.get_max_lots = AsyncMock(return_value=SimpleNamespace(
        buy_cash=2, sell_cash=0, buy_margin=10, sell_margin=10, leverage=5.0))
    runtime.broker = broker
    runtime._pos_pct = lambda: 1.0
    runtime.config.margin_sessions = ['day']
    runtime._sector_meta = {}
    return broker


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['equity', 'free_funds', 'get_max_lots'])
async def test_live_unknown_budget_or_limit_cannot_use_test_capital(runtime, failure):
    broker = use_offline_live_broker(runtime)
    getattr(broker, failure).side_effect = RuntimeError('offline unavailable')
    runtime.config.ensemble_capital = 1000000
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    runtime.portfolio_snapshot.assert_not_awaited()
    broker.cash.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_cash_cap_applies_with_margin_disabled(runtime):
    broker = use_offline_live_broker(runtime)
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY',
                                {'momentum': True, 'lot_size': 999, 'requested_lots': 999})
    assert runtime.pending_orders['TEST'].meta['lot_size'] == 1
    assert runtime.pending_orders['TEST'].meta['requested_lots'] == 2
    assert runtime.pending_orders['TEST'].qty == 2
    broker.get_max_lots.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_zero_cash_limit_cannot_borrow_when_margin_disabled(runtime):
    broker = use_offline_live_broker(runtime)
    await runtime._submit_order('TEST', 'TEST', 'open', 'SELL', {'momentum': True})
    assert runtime.pending_orders == {}
    runtime.portfolio_snapshot.assert_not_awaited()
    broker.get_max_lots.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('cash', [0.0, -10.0, float('nan')])
async def test_paper_lost_capital_is_not_recreated_from_initial(runtime, cash):
    from app.bot.paper_broker import PaperBroker

    broker = PaperBroker.__new__(PaperBroker)
    broker.ensure_account = AsyncMock(return_value=SimpleNamespace(cash=cash))
    broker.positions = AsyncMock(return_value=[])
    broker.get_max_lots = AsyncMock()
    runtime.broker = broker
    runtime._pos_pct = lambda: 1.0
    runtime.config.initial_cash = 20000
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    broker.get_max_lots.assert_not_awaited()
    runtime.portfolio_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('equity', [0.0, float('nan'), float('inf'), RuntimeError('offline')])
async def test_enabled_exposure_cap_requires_known_equity(runtime, equity):
    runtime.config.max_exposure_pct = 0.5
    runtime.broker.equity = AsyncMock()
    if isinstance(equity, Exception):
        runtime.broker.equity.side_effect = equity
    else:
        runtime.broker.equity.return_value = equity
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime._trace_order_reject.call_args.args[4] == 'exposure_unknown'
    runtime.portfolio_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_exposure_cap_uses_confirmed_lot_size(runtime):
    runtime.universe = [{'figi': 'TEST', 'lot_size': 10}]
    runtime.config.ensemble_capital = 2000
    runtime.config.max_exposure_pct = 0.5
    runtime.broker.equity = AsyncMock(return_value=3000)
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime._trace_order_reject.call_args.args[4] == 'max_exposure'
    runtime.portfolio_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_exposure_cap_allows_known_affordable_entry(runtime):
    runtime.config.max_exposure_pct = 0.5
    runtime.broker.equity = AsyncMock(return_value=1000)
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders['TEST'].qty == 1


@pytest.mark.asyncio
async def test_exposure_cap_cannot_treat_unknown_held_position_as_zero(runtime):
    runtime.config.max_exposure_pct = 0.5
    runtime.broker.equity = AsyncMock(return_value=1000)
    runtime._held = {'OTHER'}
    runtime._exit_entry_px = {}
    runtime._exit_qty = {}
    runtime._pos_leverage = {}
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime._trace_order_reject.call_args.args[4] == 'exposure_unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('attributes', [
    {}, {'liquid': 1000}, {'liquid': 1000, 'starting_margin': None},
    {'liquid': 1000, 'starting_margin': float('nan')}, RuntimeError('offline'),
])
async def test_live_account_exposure_cannot_fall_back_to_empty_local_book(runtime, attributes):
    broker = use_offline_live_broker(runtime)
    runtime.config.max_exposure_pct = 0.5
    broker.margin_attributes = AsyncMock()
    if isinstance(attributes, Exception):
        broker.margin_attributes.side_effect = attributes
    else:
        broker.margin_attributes.return_value = attributes
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime._trace_order_reject.call_args.args[4] == 'exposure_unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('committed,expected', [(400, False), (0, True)])
async def test_live_account_cap_uses_broker_commitment_with_empty_bot_book(runtime, committed, expected):
    broker = use_offline_live_broker(runtime)
    runtime.config.max_exposure_pct = 0.5
    broker.margin_attributes = AsyncMock(return_value={'liquid': 1000, 'starting_margin': committed})
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert bool(runtime.pending_orders) is expected
    if not expected:
        assert runtime._trace_order_reject.call_args.args[4] == 'max_exposure'


@pytest.mark.asyncio
async def test_live_exposure_uses_shared_margin_budget_boundary(runtime):
    broker = use_offline_live_broker(runtime)
    runtime.config.max_exposure_pct = 1.0
    broker.margin_attributes = AsyncMock(return_value={'liquid': 1000, 'starting_margin': 800})
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY', {'momentum': True})
    assert runtime.pending_orders == {}
    assert runtime._trace_order_reject.call_args.args[4] == 'max_exposure'
