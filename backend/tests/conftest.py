"""Общие фикстуры тестов.

Hermetic-тесты не должны зависеть от операторского `configs/regime.json`:
провайдер режима по умолчанию в тестах — legacy, тесты v2 включают его явно.
Runtime/реплей/бэктест читают реальный файл (на время теста V2 там v2).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_regime_provider_legacy(monkeypatch, tmp_path_factory):
    from app.services.regime_v2 import active
    cfg = tmp_path_factory.mktemp("regime_cfg") / "regime.json"
    cfg.write_text('{"provider": "legacy", "v2_window": 12}', encoding="utf-8")
    monkeypatch.setattr(active, "CONFIG_PATH", cfg)
