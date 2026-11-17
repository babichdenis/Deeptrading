import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.bot.universe.move_select import (
    LiquidityEvidenceUnavailable, MoveSelectConfig, select_move_universe, try_select_move_universe)


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf')])
def test_invalid_turnover_threshold_rejected(value):
    with pytest.raises(ValueError):
        MoveSelectConfig(min_turnover=value).validate()


@pytest.mark.asyncio
async def test_requested_liquidity_filter_never_silently_runs_without_evidence(tmp_path):
    now = datetime.now(timezone.utc)
    cfg = MoveSelectConfig(enabled=True, min_turnover=1000000)
    with pytest.raises(LiquidityEvidenceUnavailable):
        await select_move_universe(None, {}, cfg, now)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(enabled=True, replay_only=True, min_turnover=1000000)))
    with pytest.raises(LiquidityEvidenceUnavailable):
        await try_select_move_universe(None, {}, feed='replay', as_of=now, config_path=path)


@pytest.mark.asyncio
async def test_runtime_does_not_replace_required_liquidity_filter_with_legacy(monkeypatch, tmp_path):
    import app.config as config
    import app.bot.runtime as runtime

    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(enabled=True, replay_only=True, min_turnover=1000000)))
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(move_select_config=str(path), move_select_enabled=True))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(runtime, 'SessionLocal', Session)
    with pytest.raises(LiquidityEvidenceUnavailable):
        await runtime._move_select_universe({}, SimpleNamespace(feed='replay'),
            datetime.now(timezone.utc), lambda *args: None)
