from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.gates import run_gate_chain as actual_gate_chain
from tests.test_runtime_portfolio_failure import runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('condition,reason', [
    ('paused', 'entries_paused'),
    ('session', 'session_filter'),
    ('long_disabled', 'long_disabled'),
    ('short_disabled', 'short_disabled'),
    ('daily_loss', 'risk_loss_limit'),
    ('loss_streak', 'loss_streak_hold'),
    ('held', 'already_held'),
])
async def test_actual_time_gates_block_before_broker(runtime, monkeypatch, condition, reason):
    from app.bot import gates

    monkeypatch.setattr(gates, 'run_gate_chain', actual_gate_chain)
    runtime._reject_entry = Mock()
    runtime._daily_turnover = AsyncMock(side_effect=AssertionError('Must not reach market reads'))
    side = 'BUY'
    if condition == 'paused':
        runtime.entries_paused = True
    elif condition == 'session':
        runtime.config.sessions = ['evening']
    elif condition == 'long_disabled':
        runtime.config.long_allowed = False
    elif condition == 'short_disabled':
        runtime.config.short_allowed = False
        side = 'SELL'
    elif condition == 'daily_loss':
        runtime.risk_snapshot = lambda: SimpleNamespace(
            entries_allowed=lambda: False, state='LOSS_LIMIT', daily_pnl=-1000)
    elif condition == 'loss_streak':
        runtime._loss_streak_block = lambda _: (True, 'offline loss streak')
    elif condition == 'held':
        runtime._held.add('TEST')
    await runtime._submit_order('TEST', 'TEST', 'open', side)
    assert runtime.pending_orders == {}
    assert runtime.orders == []
    assert runtime._reject_entry.call_args.args[:2] == ('time', reason)
    runtime.portfolio_snapshot.assert_not_awaited()
    runtime._daily_turnover.assert_not_awaited()
    runtime.broker.open_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('book', [None, {'state': 'empty'}, {
    'state': 'ok', 'ts': '2000-01-01T00:00:00+00:00',
    'imbalance': 0.0, 'spread_bps': 1.0,
}])
async def test_actual_book_gate_blocks_before_sizing(runtime, monkeypatch, book):
    from app.bot import gates
    from app.services import orderbook

    monkeypatch.setattr(gates, 'run_gate_chain', actual_gate_chain)
    fetch = AsyncMock(return_value=book)
    monkeypatch.setattr(orderbook, 'fetch_orderbook', fetch)
    runtime.config.feed = 'live'
    runtime.config.entry_news_blackout = False
    runtime.config.entry_ob_imbalance_max = 0.3
    runtime.config.entry_ob_spread_max = 25.0
    runtime._daily_turnover = AsyncMock(return_value=100000000)
    runtime._reject_entry = Mock()
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY')
    assert runtime.pending_orders == {}
    assert runtime.orders == []
    assert runtime._reject_entry.call_args.args[:2] == ('signal', 'orderbook_error')
    fetch.assert_awaited_once()
    runtime.portfolio_snapshot.assert_not_awaited()
    runtime.broker.open_position.assert_not_awaited()


def enable_actual_trend_path(runtime, monkeypatch):
    from app.bot import gates

    monkeypatch.setattr(gates, 'run_gate_chain', actual_gate_chain)
    runtime._reject_entry = Mock()
    runtime.mtf_macd_map = AsyncMock(return_value={})
    runtime.daily_bias_map = AsyncMock(return_value={})
    runtime.trade_history = AsyncMock(return_value=[])
    runtime._rank_ok = lambda ticker: (True, '')
    runtime.hm_map = AsyncMock(return_value={})
    runtime.config.rank_enabled = True


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,reason', [
    ('rank', 'rank_data_error'), ('hm', 'hm_data_error'), ('sector', 'sector_data_error'),
])
async def test_enabled_gate_data_failure_blocks_entry(runtime, monkeypatch, failure, reason):
    enable_actual_trend_path(runtime, monkeypatch)
    if failure == 'rank':
        runtime.trade_history.side_effect = RuntimeError('offline unavailable')
    elif failure == 'hm':
        runtime.config.entry_hm_veto = True
        runtime.hm_map.side_effect = RuntimeError('offline unavailable')
    else:
        runtime.config.max_sector_positions = 2
        runtime.sector_meta.side_effect = RuntimeError('offline unavailable')
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY')
    assert runtime.pending_orders == {}
    assert runtime._reject_entry.call_args.args[1] == reason
    runtime.portfolio_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_rank_and_hm_do_not_require_their_data(runtime, monkeypatch):
    enable_actual_trend_path(runtime, monkeypatch)
    runtime.config.rank_enabled = False
    runtime.config.entry_hm_veto = False
    runtime.trade_history.side_effect = RuntimeError('must not fetch')
    runtime.hm_map.side_effect = RuntimeError('must not fetch')
    await runtime._submit_order('TEST', 'TEST', 'open', 'BUY')
    assert runtime.pending_orders['TEST'].qty == 1
    runtime.trade_history.assert_not_awaited()
    runtime.hm_map.assert_not_awaited()
