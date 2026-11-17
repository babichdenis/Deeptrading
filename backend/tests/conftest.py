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


_MISSING = (FileNotFoundError, KeyError)


def _artifact_marker(node) -> bool:
    return node.get_closest_marker("artifact") is not None


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Тест с маркером `artifact` без research-артефакта пропускается.

    Маркер объявлен в pytest.ini как «требует локальные research-артефакты
    backend/reports/ (не в репо)». Если такого файла нет, это не регрессия кода,
    а отсутствие входных данных: падение скрыло бы настоящие ошибки.
    """
    try:
        return (yield)
    except _MISSING as exc:
        if _artifact_marker(request.node):
            pytest.skip(f"нет research-артефакта: {exc}")
        raise


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    try:
        return (yield)
    except _MISSING as exc:
        if _artifact_marker(item):
            pytest.skip(f"нет research-артефакта: {exc}")
        raise
