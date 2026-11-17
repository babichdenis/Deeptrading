import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_margin_switch_survives_patch_save_and_reload(monkeypatch, tmp_path, enabled):
    core = importlib.import_module('app.bot.runtime')
    route = importlib.import_module('app.api.routes.bot')
    gates = importlib.import_module('app.bot.gates')
    monkeypatch.setattr(core, '_BOT_CONFIG_FILE', str(tmp_path / 'bot_config.json'))
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = SimpleNamespace(value={})
    monkeypatch.setattr(core, 'SessionLocal', lambda: session)
    monkeypatch.setattr(route, 'runtime', SimpleNamespace(running=False, starting=False, _log=lambda *args: None))
    monkeypatch.setattr(gates, 'save_gates_config', lambda *args, **kwargs: None)
    initial = core.BotConfig()
    initial.use_margin = not enabled
    await core.save_bot_settings(initial)
    result = await route.bot_config_patch(route.BotConfigPatch(use_margin=enabled))
    assert result['use_margin'] is enabled
    saved = await core.load_bot_settings()
    assert saved['use_margin'] is enabled
    for mode in ('live', 'sandbox', 'test'):
        cfg = await route._cfg_from_saved(mode=mode)
        assert cfg.use_margin is enabled
        assert route._config_payload(cfg)['use_margin'] is enabled
