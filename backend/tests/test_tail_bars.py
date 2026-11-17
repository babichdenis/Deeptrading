"""tail_bars — быстрый хвост ряда из зоны хаба.

Потребителям нужен ряд на расчёт (ATR, отбор), а не диагностика покрытия:
read_range считает missing/unexpected по всем минутам, и на 165 бумагах за цикл
это дороже самих данных. tail_bars читает хвост напрямую, но остаётся в зоне
хаба: закрытость бара и политика входа применяются те же.

Здесь проверяется то, что можно проверить без БД: маппинг ТФ, валидация
аргументов, граница закрытия бара. Само чтение проверяется на живой БД отдельно.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.services.candle_hub_orchestrator import (
    CandleHubOrchestrator,
    _bar_close_ts,
    tf_to_interval,
)

AS_OF = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)


def test_tf_maps_to_its_own_db_interval() -> None:
    assert tf_to_interval("1min") == 1
    assert tf_to_interval("5min") == 2
    assert tf_to_interval("10min") == 8
    assert tf_to_interval("hour") == 4
    assert tf_to_interval("4h") == 11
    assert tf_to_interval("day") == 24


def test_derived_tf_is_refused_not_silently_wrong() -> None:
    """У 15min/30min/2h нет своего ряда — молча читать 1min было бы подменой."""
    for tf in ("15min", "30min", "2h"):
        with pytest.raises(ValueError):
            tf_to_interval(tf)


def test_unknown_tf_is_refused() -> None:
    with pytest.raises(ValueError):
        tf_to_interval("60min")


def test_bar_close_is_start_plus_tf() -> None:
    """ts — НАЧАЛО бакета: минутный бар 10:00 закрыт в 10:01."""
    assert _bar_close_ts(AS_OF, 60) == AS_OF + timedelta(minutes=1)
    assert _bar_close_ts(AS_OF, 300) == AS_OF + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_rejects_bad_n() -> None:
    orch = CandleHubOrchestrator()
    with pytest.raises(ValueError):
        await orch.tail_bars(None, "SBER", "5min", 0, as_of=AS_OF)


@pytest.mark.asyncio
async def test_rejects_naive_as_of() -> None:
    orch = CandleHubOrchestrator()
    with pytest.raises(ValueError):
        await orch.tail_bars(None, "SBER", "5min", 10, as_of=datetime(2026, 11, 15, 16, 0))


@pytest.mark.asyncio
async def test_rejects_non_datetime_as_of() -> None:
    orch = CandleHubOrchestrator()
    with pytest.raises(ValueError):
        await orch.tail_bars(None, "SBER", "5min", 10, as_of="2026-11-15")


def _called_names(func) -> set[str]:
    """Имена вызываемых функций/методов в теле — без учёта докстринга."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def test_tail_does_not_compute_coverage() -> None:
    """Быстрый путь не должен тянуть диагностику диапазона."""
    called = _called_names(CandleHubOrchestrator.tail_bars)
    for heavy in ("read_range", "read_history_range", "read_last_bars"):
        assert heavy not in called, f"tail_bars не должен вызывать {heavy}"


def test_tail_applies_ingest_policy() -> None:
    """Читатель не обходит политику входа хаба."""
    src = inspect.getsource(CandleHubOrchestrator.tail_bars)
    assert "validate_candle" in src
    assert "_normalize_engine_candle" in src


def test_tail_drops_unclosed_bucket() -> None:
    src = inspect.getsource(CandleHubOrchestrator.tail_bars)
    assert "_bar_close_ts" in src, "незакрытый бакет обязан отсекаться по ts + TF"
