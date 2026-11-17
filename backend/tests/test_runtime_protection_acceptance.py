from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.runtime import PaperBotRuntime
from app.engine.models import Candle


@pytest.mark.asyncio
@pytest.mark.parametrize('side,stop,new_stop,low,high,close', [
    ('LONG', 95.0, 102.0, 99.0, 105.0, 104.0),
    ('SHORT', 105.0, 98.0, 95.0, 101.0, 96.0),
])
async def test_runtime_does_not_exit_on_stop_created_by_same_bar(
    side, stop, new_stop, low, high, close,
):
    figi = 'OFFLINE_FIGI'
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
                 open=100.0, high=high, low=low, close=close)
    policy = SimpleNamespace(
        update_stop=lambda *args, **kwargs: new_stop,
        trailing_activated=lambda *args: True,
        trail_activation_comm_mult=4.0,
    )
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._replay_cur = None
    rt._replay_from = None
    rt.config = SimpleNamespace(commission_rate=0.0005, slippage_bps=0.0, params={})
    rt._in_trading_session = lambda: True
    rt._log = lambda *args, **kwargs: None
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    rt._st_update_sl = AsyncMock()
    rt.atr_now = lambda *args: 1.0
    rt._exit_plans = {figi: policy}
    rt._exit_side = {figi: side}
    rt._exit_entry_px = {figi: 100.0}
    rt._exit_qty = {figi: 10}
    rt._trail_stop = {figi: stop}
    rt._trail_active = {figi: True}
    rt._exit_target = {}
    rt._peak_pnl = {}
    rt._gb_peak = {}
    rt.buffers = {figi: deque([bar])}
    rt.broker = SimpleNamespace(close_position=AsyncMock(return_value=None))
    pos = SimpleNamespace(side=side, entry_price=100.0, qty=10, ticker='TEST')

    assert await rt._step_exit(figi, bar, pos) is False
    rt.broker.close_position.assert_not_awaited()
    assert rt._trail_stop[figi] == new_stop

    next_price = 101.0 if side == 'LONG' else 99.0
    next_bar = Candle(ts=datetime(2026, 11, 15, 12, 1, tzinfo=timezone.utc),
                      open=next_price, high=next_price, low=next_price, close=next_price)
    rt.buffers[figi].append(next_bar)
    assert await rt._step_exit(figi, next_bar, pos) is False
    rt.broker.close_position.assert_awaited_once()
    assert rt.broker.close_position.await_args.args == (figi, next_price, 'stop_loss')


@pytest.mark.asyncio
@pytest.mark.parametrize("executed_qty", [4, 10])
@pytest.mark.parametrize("pending_action,pending_status", [
    (None, None), ('open', 'PENDING_RECONCILIATION'), ('close', 'PENDING_RECONCILIATION'),
    ('open', 'PARTIAL_FILLED'), ('close', 'PARTIAL_FILLED'),
])
async def test_runtime_records_broker_exit_price_without_synthetic_slippage(pending_action, pending_status, executed_qty):
    figi = 'OFFLINE_FIGI'
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
                 open=94.0, high=94.5, low=93.0, close=94.0)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._replay_cur = None
    rt._replay_from = None
    rt.config = SimpleNamespace(commission_rate=0.0005, slippage_bps=100.0, params={})
    rt._in_trading_session = lambda: True
    rt._log = lambda *args, **kwargs: None
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    rt.atr_now = lambda *args: 1.0
    rt._exit_plans = {figi: SimpleNamespace()}
    rt._exit_side = {figi: 'LONG'}
    rt._exit_entry_px = {figi: 100.0}
    rt._exit_qty = {figi: 10}
    rt._trail_stop = {figi: 95.0}
    rt._trail_active = {figi: True}
    rt._exit_target = {}
    rt._peak_pnl = {}
    rt.buffers = {figi: deque([bar])}
    trade = SimpleNamespace(exit_price=93.5, qty=executed_qty, net_pnl=-6.5 * executed_qty)
    rt.broker = SimpleNamespace(close_position=AsyncMock(return_value=trade))
    rt._held = {figi}
    rt._entry_bar_index = {figi: 0}
    rt._bar_counter = 1
    rt._opposite_count = {}
    rt._last_exit_bar = {}
    rt._clear_exit_state = lambda *args: None
    rt._st_close = AsyncMock()
    rt._check_circuit_breaker = AsyncMock()
    pos = SimpleNamespace(side='LONG', entry_price=100.0, qty=10, ticker='TEST')
    if pending_action is not None:
        pending = SimpleNamespace(action=pending_action, status=pending_status)
        rt.pending_orders = {figi: pending}
        for _ in range(2):
            assert await rt._step_exit(figi, bar, pos) is False
        rt.broker.close_position.assert_not_awaited()
        rt._st_close.assert_not_awaited()
        assert rt.pending_orders[figi] is pending
        assert figi in rt._held
        return
    if executed_qty < pos.qty:
        assert await rt._step_exit(figi, bar, pos) is False
        assert figi in rt._held
        assert figi in rt._exit_plans
        rt._st_close.assert_not_awaited()
        await rt._step_exit(figi, bar, pos)
        rt.broker.close_position.assert_awaited_once()
        return
    assert await rt._step_exit(figi, bar, pos) is True
    rt.broker.close_position.assert_awaited_once_with(figi, 94.0, 'stop_loss')
    assert rt._st_close.await_args.args == (figi, 93.5)
    assert rt._st_close.await_args.kwargs['meta']['exit_price'] == 93.5
