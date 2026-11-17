from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.bot.execution_reference import ExecutionReference
from app.bot.intrabar_reference import check_intrabar_references

NOW = datetime(2026,11,16,11,tzinfo=timezone.utc)


def runtime():
    reference = ExecutionReference('F',99,NOW-timedelta(seconds=1),NOW)
    broker = SimpleNamespace(mode='sandbox',_account='A',execution_references=AsyncMock(return_value={'F':reference}))
    return SimpleNamespace(broker=broker,running=True,_exit_plans={'F':object()},_bot_now=lambda:NOW,
                           _intrabar_handle=AsyncMock(return_value='closed'),entries_paused=True)


@pytest.mark.asyncio
async def test_fresh_reference_checks_protection_even_when_entries_paused():
    rt=runtime()
    assert await check_intrabar_references(rt)=={'F':'closed'}
    rt._intrabar_handle.assert_awaited_once_with('F',99)


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['stale','future','wrong_figi','missing','account','plan'])
async def test_invalid_or_raced_reference_never_triggers_close(case):
    rt=runtime()
    async def fetch(figis):
        if case=='account':rt.broker._account='B'
        if case=='plan':rt._exit_plans['F']=object()
        if case=='missing':return {}
        return {'F':ExecutionReference('OTHER' if case=='wrong_figi' else 'F',99,
             NOW+timedelta(seconds=1) if case=='future' else NOW-timedelta(seconds=31 if case=='stale' else 1),NOW)}
    rt.broker.execution_references.side_effect=fetch
    await check_intrabar_references(rt)
    rt._intrabar_handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['elapsed','removed_plan'])
async def test_reference_revalidated_after_waiting_for_candle_lock(change):
    import asyncio
    from app.bot.pending_dispatch import execution_lock
    rt = runtime()
    fetched = asyncio.Event()
    references = rt.broker.execution_references.return_value
    async def fetch(figis):
        fetched.set()
        return references
    rt.broker.execution_references.side_effect = fetch
    lock = execution_lock(rt)
    await lock.acquire()
    task = asyncio.create_task(check_intrabar_references(rt))
    await fetched.wait()
    if change == 'elapsed':
        rt._bot_now = lambda: NOW+timedelta(seconds=40)
    else:
        rt._exit_plans.clear()
    lock.release()
    await task
    rt._intrabar_handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_path_and_close_all_submit_unknown_close_once():
    import asyncio
    from tests.test_runtime_close_all_acceptance import close_fixture
    rt, figi = close_fixture('unknown')
    rt.running = True
    rt.broker.mode, rt.broker._account = 'sandbox', 'A'
    rt._trail_stop, rt._exit_target = {figi:100}, {}
    now = rt._bot_now()
    rt.broker.execution_references = AsyncMock(return_value={figi:ExecutionReference(figi,99,now,now)})
    await asyncio.gather(check_intrabar_references(rt),rt.close_all())
    assert rt.broker.close_position.await_count == 1
    assert figi in rt._held and figi in rt._exit_plans
    rt._st_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_intrabar_loop_uses_timestamped_adapter(monkeypatch):
    import asyncio
    from app.bot.live_broker import LiveBroker
    from app.bot.runtime import PaperBotRuntime
    rt = runtime()
    rt.config = SimpleNamespace(intrabar_check_sec=10)
    rt.broker = LiveBroker.__new__(LiveBroker)
    rt.broker.last_prices = AsyncMock()
    probe = AsyncMock(return_value={})
    monkeypatch.setattr('app.bot.intrabar_reference.check_intrabar_references',probe)
    calls = 0
    async def sleep(seconds):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise asyncio.CancelledError()
    monkeypatch.setattr('app.bot.runtime.asyncio.sleep',sleep)
    with pytest.raises(asyncio.CancelledError):
        await PaperBotRuntime._intrabar_exit_loop(rt)
    probe.assert_awaited_once_with(rt)
    rt.broker.last_prices.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_diagnostics_show_stale_recovery_without_repeated_events():
    from unittest.mock import Mock
    rt = runtime()
    rt.events = SimpleNamespace(log=Mock())
    rt.broker.execution_references.return_value = {'F':ExecutionReference('F',99,NOW-timedelta(seconds=31),NOW)}
    await check_intrabar_references(rt)
    await check_intrabar_references(rt)
    assert rt._intrabar_reference_status['F']['reason'] == 'execution_reference_stale'
    assert rt.events.log.call_count == 1
    rt.broker.execution_references.return_value = {'F':ExecutionReference('F',99,NOW,NOW)}
    await check_intrabar_references(rt)
    assert rt._intrabar_reference_status['F']['reason'] == 'reference_ready'
    assert rt.events.log.call_count == 2
    rt.broker._account = 'B'
    rt.broker.execution_references.return_value = {}
    await check_intrabar_references(rt)
    assert rt._intrabar_reference_scope == ('sandbox','B')
    assert rt._intrabar_reference_status['F']['market_at'] is None


@pytest.mark.asyncio
async def test_missing_timestamp_is_reported_without_crashing_diagnostics():
    rt = runtime()
    rt.broker.execution_references.return_value = {'F':ExecutionReference('F',99,None,NOW)}
    await check_intrabar_references(rt)
    assert rt._intrabar_reference_status['F']['reason'] == 'execution_reference_requires_aware_time'
    assert rt._intrabar_reference_status['F']['market_at'] is None
    rt._intrabar_handle.assert_not_awaited()
