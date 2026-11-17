"""Intrabar protection must clear state only on a confirmed full close."""
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.live_broker import LiveBroker, LivePosition
from app.bot.runtime import PaperBotRuntime

ROOT = Path("reports/connected_week_20261109_13_c1")
FIGI = 'OFFLINE_INTRABAR_TEST'


def make_runtime(outcome, qty=20, stop=100.0, price=99.0):
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    pos = LivePosition(FIGI, 'TEST', 'LONG', qty, 100, now, None, None)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(commission_rate=0.0005, slippage_bps=0, params={})
    rt._bot_now = lambda: now
    rt._in_trading_session = lambda: True
    rt._session_gate_log = Mock()
    rt._log = Mock()
    rt.events = SimpleNamespace(log=Mock())
    rt.tickers = {FIGI: 'TEST'}
    rt.buffers = {}
    rt._held = {FIGI}
    rt._exit_plans = {FIGI: object()}
    rt._trail_stop = {FIGI: stop}
    rt._exit_target = {FIGI: None}
    rt._exit_side = {FIGI: 'LONG'}
    rt._exit_qty = {FIGI: qty}
    rt._entry_bar_index = {FIGI: 1}
    rt._bar_counter = 5
    rt._opposite_count = {FIGI: 2}
    rt._last_exit_bar = {}
    rt._peak_pnl = {FIGI: {'pnl': 1.0}}
    rt.pending_orders = {}
    rt._st_close = AsyncMock()
    rt._clear_exit_state = Mock(side_effect=lambda key: rt._exit_plans.pop(key, None))
    broker = LiveBroker.__new__(LiveBroker)
    broker.get_position = AsyncMock(return_value=pos)
    broker._get_lot = AsyncMock(return_value=1)

    async def close(*args, **kwargs):
        await asyncio.sleep(0)
        if outcome == 'timeout':
            raise TimeoutError('offline unknown result')
        if outcome == 'unknown':
            return None
        units = qty // 2 if outcome == 'partial' else qty
        return SimpleNamespace(qty=units, exit_price=price, net_pnl=-float(units),
                               client_order_id=kwargs.get('client_order_id'),
                               broker_order_id='OFFLINE_ORDER', id=7)

    broker.close_position = AsyncMock(side_effect=close)
    rt.broker = broker
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown', 'timeout', 'partial', 'full'])
async def test_intrabar_clears_only_on_confirmed_full_close(outcome):
    rt = make_runtime(outcome)
    status = await rt._intrabar_handle(FIGI, 99.0)
    full = outcome == 'full'
    assert status == ('closed' if full else ('pending_partial' if outcome == 'partial' else 'pending_unknown'))
    assert (FIGI in rt._held) == (not full)
    assert (FIGI in rt._exit_plans) == (not full)
    assert rt._clear_exit_state.call_count == int(full)
    if full:
        rt._st_close.assert_awaited_once()
        assert FIGI not in rt.pending_orders
        assert rt._last_exit_bar[FIGI] == 5
    else:
        rt._st_close.assert_not_awaited()
        assert FIGI in rt.pending_orders
        assert rt._trail_stop[FIGI] == 100.0
        assert FIGI in rt._entry_bar_index
        if outcome == 'partial':
            assert rt._exit_qty[FIGI] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unknown', 'timeout', 'partial'])
async def test_intrabar_does_not_resubmit_while_unresolved(outcome):
    rt = make_runtime(outcome)
    await rt._intrabar_handle(FIGI, 99.0)
    await rt._intrabar_handle(FIGI, 99.0)
    assert rt.broker.close_position.await_count == 1
    assert FIGI in rt._held and FIGI in rt._exit_plans


@pytest.mark.asyncio
async def test_intrabar_ignores_price_above_stop():
    rt = make_runtime('full', stop=90.0)
    assert await rt._intrabar_handle(FIGI, 99.0) == 'no_hit'
    assert rt.broker.close_position.await_count == 0
    assert FIGI in rt._held


def main():
    cases = {outcome: asyncio.run(_collect(outcome)) for outcome in ('unknown', 'timeout', 'partial', 'full')}
    tag = (sys.argv[1] if len(sys.argv) > 1 else '').strip()
    if not tag:
        raise SystemExit('usage: test_intrabar_close_ack.py <tag>')
    payload = {'figi': FIGI, 'cases': cases}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + '\n'
    path = ROOT / f'intrabar_close_ack_cases_{tag}.json'
    with open(path, 'xb') as handle:
        handle.write(text.encode('utf-8'))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps({'artifact': {'path': str(path), 'tag': tag, 'sha256': digest,
                                   'bytes': path.stat().st_size},
                      'cases': cases}, ensure_ascii=False))


async def _collect(outcome):
    rt = make_runtime(outcome)
    status = await rt._intrabar_handle(FIGI, 99.0)
    return {
        'status': status,
        'held': FIGI in rt._held,
        'plan_kept': FIGI in rt._exit_plans,
        'clear_calls': rt._clear_exit_state.call_count,
        'st_close_awaited': rt._st_close.await_count,
        'pending_order': FIGI in rt.pending_orders,
        'stop_kept': rt._trail_stop.get(FIGI),
        'entry_index_kept': FIGI in rt._entry_bar_index,
        'residual_qty': rt._exit_qty.get(FIGI),
        'submitted': rt.broker.close_position.await_count,
    }


if __name__ == '__main__':
    main()
