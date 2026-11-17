import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
async def test_stop_joins_dispatch_even_when_main_task_already_finished():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def dispatch():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    task = asyncio.create_task(dispatch())
    await entered.wait()
    rt = SimpleNamespace(running=True, _close_trace=AsyncMock(), feed=None,
        stream_manager=None, broker=SimpleNamespace(close=Mock()),
        startup_task=None, task=None, _dispatch_task=task,
        _flush_persist_once=AsyncMock(), events=SimpleNamespace(log=Mock()))
    try:
        result = await PaperBotRuntime.stop(rt)
        assert result == {'stopped': True, 'was_running': True}
        assert task.done() and cancelled.is_set()
        assert not rt.running and not rt.starting
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
