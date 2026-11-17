from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routes import bot
from app.services import run_state


@pytest.mark.asyncio
@pytest.mark.parametrize('previous,target', list(permutations(('sandbox', 'live', 'test'), 2)))
async def test_selecting_mode_keeps_stopped_runtime_stopped(monkeypatch, previous, target):
    stored = {'mode': previous}
    runtime = SimpleNamespace(running=False, starting=False, start=AsyncMock(), stop=AsyncMock(),
                              config=SimpleNamespace(mode=previous), _preset_conflicts=[])
    monkeypatch.setattr(bot, 'runtime', runtime)
    monkeypatch.setattr(bot, '_write_env_mode', lambda mode: stored.update(env_mode=mode))
    monkeypatch.setattr(bot, '_save_test_sidecar', lambda *args: None)
    monkeypatch.setattr(bot, '_cfg_from_saved', AsyncMock(return_value=SimpleNamespace(mode=target)))
    monkeypatch.setattr(run_state, 'save_run_state', lambda state: stored.update(state) or True)
    monkeypatch.setattr(run_state, 'sync_env', lambda state: None)
    response = await bot.bot_set_mode(bot.ModeRequest(mode=target))
    assert response['mode'] == target
    runtime.start.assert_not_awaited()
    runtime.stop.assert_not_awaited()
    assert runtime.running is False


def isolated_selection(monkeypatch, stored, save_ok=True):
    runtime = SimpleNamespace(running=False, starting=False, start=AsyncMock(), stop=AsyncMock(),
                              config=SimpleNamespace(mode=stored['mode']), _preset_conflicts=[])
    env_writes = []
    monkeypatch.setattr(bot, 'runtime', runtime)
    monkeypatch.setattr(bot, '_write_env_mode', env_writes.append)
    monkeypatch.setattr(bot, '_save_test_sidecar', lambda *args: None)
    monkeypatch.setattr(bot, '_cfg_from_saved', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(run_state, 'load_run_state', lambda *args, **kwargs: dict(stored))
    monkeypatch.setattr(run_state, 'save_run_state',
                        lambda state: (stored.update(state) or True) if save_ok else False)
    monkeypatch.setattr(run_state, 'sync_env', lambda state: None)
    return runtime, env_writes


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["live", "test"])
async def test_mode_selection_preserves_saved_replay_parameters(monkeypatch, target):
    stored = dict(mode='test', test_name='kept_run', replay_start='2026-09-01T00:00:00Z',
                  replay_end='2026-09-02T00:00:00Z', test_engine='rsi', test_interval='5min',
                  test_params={'period': 14}, preset={'strategy_id': 'rsi'})
    previous = dict(stored)
    isolated_selection(monkeypatch, stored)
    await bot.bot_set_mode(bot.ModeRequest(mode=target))
    assert stored['mode'] == target
    for key, value in previous.items():
        if key != 'mode':
            assert stored[key] == value, key


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_mode_persistence_cannot_report_success_or_change_env(monkeypatch, raises):
    from fastapi import HTTPException

    stored = {'mode': 'sandbox'}
    runtime, env_writes = isolated_selection(monkeypatch, stored, save_ok=False)
    if raises:
        def failed_save(state):
            raise OSError('offline disk unavailable')
        monkeypatch.setattr(run_state, 'save_run_state', failed_save)
    with pytest.raises(HTTPException):
        await bot.bot_set_mode(bot.ModeRequest(mode='live'))
    assert stored == {'mode': 'sandbox'}
    assert env_writes == []
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['running', 'starting', 'PENDING_RECONCILIATION', 'PARTIAL_FILLED'])
async def test_mode_change_rejects_active_or_unresolved_context(monkeypatch, state):
    from fastapi import HTTPException

    stored = {'mode': 'live'}
    runtime, env_writes = isolated_selection(monkeypatch, stored)
    runtime.running = state == 'running'
    runtime.starting = state == 'starting'
    runtime.pending_orders = {}
    if state in ('PENDING_RECONCILIATION', 'PARTIAL_FILLED'):
        runtime.pending_orders['OFFLINE_FIGI'] = SimpleNamespace(status=state)
    with pytest.raises(HTTPException) as exc:
        await bot.bot_set_mode(bot.ModeRequest(mode='sandbox'))
    assert exc.value.status_code == 409
    assert stored == {'mode': 'live'}
    assert env_writes == []
    runtime.start.assert_not_awaited()
    runtime.stop.assert_not_awaited()
