import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.execution_reference import ExecutionReference
from app.bot.pending_dispatch import dispatch_pending, execution_lock

NOW = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)


def runtime():
    order = SimpleNamespace(id='REQUEST', status='SUBMITTED', action='open', created_at=NOW)
    reference = ExecutionReference('FIGI', 100, NOW, NOW)
    broker = SimpleNamespace(mode='sandbox', _account='OFFLINE',
                             execution_references=AsyncMock(return_value={'FIGI': reference}))
    rt = SimpleNamespace(events=SimpleNamespace(log=Mock()), broker=broker, pending_orders={'FIGI': order},
                         entries_paused=False, running=True, _bot_now=lambda: NOW)
    async def execute(figi, ref):
        rt.pending_orders.pop(figi)
    rt._execute_pending = AsyncMock(side_effect=execute)
    return rt, order, reference


@pytest.mark.asyncio
async def test_dispatches_without_waiting_for_another_closed_candle():
    rt, order, ref = runtime()
    await dispatch_pending(rt)
    rt._execute_pending.assert_awaited_once_with('FIGI', ref)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel', 'replace', 'pause', 'stop', 'scope', 'unknown'])
async def test_changed_state_during_quote_request_cannot_dispatch(change):
    rt, order, ref = runtime()
    async def fetch(figis):
        if change == 'cancel': order.status = 'CANCELLED'
        if change == 'replace': rt.pending_orders['FIGI'] = SimpleNamespace()
        if change == 'pause': rt.entries_paused = True
        if change == 'stop': rt.running = False
        if change == 'scope': rt.broker._account = 'OTHER'
        if change == 'unknown': order.status = 'PENDING_RECONCILIATION'
        return {'FIGI': ref}
    rt.broker.execution_references.side_effect = fetch
    await dispatch_pending(rt)
    rt._execute_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_dispatches_send_only_once():
    rt, order, ref = runtime()
    await asyncio.gather(dispatch_pending(rt), dispatch_pending(rt))
    rt._execute_pending.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_waits_for_candle_processing_lock():
    rt, order, ref = runtime()
    async with execution_lock(rt):
        task = asyncio.create_task(dispatch_pending(rt))
        await asyncio.sleep(0)
        rt._execute_pending.assert_not_awaited()
    await task
    rt._execute_pending.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_or_predecision_price_leaves_order_unsent():
    rt, order, ref = runtime()
    rt.broker.execution_references.return_value = {}
    await dispatch_pending(rt)
    rt.broker.execution_references.return_value = {'FIGI': ExecutionReference('FIGI',100,NOW-timedelta(seconds=1),NOW)}
    await dispatch_pending(rt)
    rt._execute_pending.assert_not_awaited()
    assert rt.pending_orders['FIGI'] is order


@pytest.mark.asyncio
async def test_wait_reason_logged_only_on_transition_and_cleared_when_ready():
    rt, order, ref = runtime()
    rt.broker.execution_references.return_value = {}
    await dispatch_pending(rt)
    await dispatch_pending(rt)
    assert rt.events.log.call_count == 1
    assert order.meta['dispatch_state'] == 'waiting_market_reference'
    rt.broker.execution_references.return_value = {'FIGI': ref}
    await dispatch_pending(rt)
    assert rt.events.log.call_count == 2
    assert order.meta['dispatch_state'] == 'reference_ready'


@pytest.mark.asyncio
async def test_close_survives_missing_and_stale_quotes_then_dispatches_while_entries_paused():
    rt, order, ref = runtime()
    order.action = 'close'
    rt.entries_paused = True
    rt._bot_now = lambda: NOW + timedelta(minutes=1)
    rt.broker.execution_references.return_value = {}
    await dispatch_pending(rt)
    assert rt.pending_orders['FIGI'] is order
    rt._execute_pending.assert_not_awaited()
    rt.broker.execution_references.return_value = {'FIGI': ref}
    await dispatch_pending(rt)
    assert rt.pending_orders['FIGI'] is order
    assert order.meta['dispatch_state'] == 'execution_reference_stale'
    rt._execute_pending.assert_not_awaited()
    fresh = ExecutionReference('FIGI', 97, NOW + timedelta(minutes=1), NOW + timedelta(minutes=1))
    rt.broker.execution_references.return_value = {'FIGI': fresh}
    await dispatch_pending(rt)
    rt._execute_pending.assert_awaited_once_with('FIGI', fresh)
    assert 'FIGI' not in rt.pending_orders
