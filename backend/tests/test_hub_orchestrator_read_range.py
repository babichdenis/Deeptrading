"""CandleHubOrchestrator.read_range: интеграция helper'а в оркестратор (ARCH107/ARCH117).

Проверяем то, что добавляет оркестратор поверх helper'а: обязательные
tz-aware границы, запрет пустого календарного свидетельства и DB-first —
память hub в исторический ответ не попадает.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.engine.candlehub import CandleHub  # noqa: E402
from app.engine.models import Candle  # noqa: E402
from app.services.candle_hub_orchestrator import CandleHubOrchestrator  # noqa: E402

T = datetime(2026, 9, 1, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def fake_db(rows):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))


def row(i, price=100.0):
    return (T + i * M, price, price + 1, price - 1, price, 1)


def evidence(minutes: int) -> dict:
    return {"expected_minutes": (T + i * M for i in range(minutes)),
            "calendar_evidence": "synthetic-continuous-session"}


@pytest.mark.asyncio
async def test_naive_boundaries_are_rejected() -> None:
    orch = CandleHubOrchestrator()
    db = fake_db([])
    with pytest.raises(ValueError, match="timezone-aware"):
        await orch.read_range(db, "TEST", "1min", T.replace(tzinfo=None), T + M, as_of=T + M)
    with pytest.raises(ValueError, match="timezone-aware"):
        await orch.read_range(db, "TEST", "1min", T, T + M, as_of=T.replace(tzinfo=None))
    assert not db.execute.await_count, "валидация обязана отсекать до обращения к БД"


@pytest.mark.asyncio
async def test_empty_calendar_evidence_is_rejected() -> None:
    orch = CandleHubOrchestrator()
    with pytest.raises(ValueError, match="calendar_evidence"):
        await orch.read_range(fake_db([]), "TEST", "1min", T, T + M, as_of=T + M,
                              calendar_evidence="   ")


@pytest.mark.asyncio
async def test_complete_range_is_ok_with_evidence() -> None:
    orch = CandleHubOrchestrator()
    result = await orch.read_range(fake_db([row(i) for i in range(5)]), "TEST", "1min",
                                   T, T + 5 * M, as_of=T + 5 * M, **evidence(5))
    assert result.status == "ok"
    assert result.coverage_known is True
    assert [c.ts for c in result.candles] == [T + i * M for i in range(5)]


@pytest.mark.asyncio
async def test_unknown_calendar_is_partial_not_ok() -> None:
    """Без календарного свидетельства полный набор баров всё равно partial."""
    orch = CandleHubOrchestrator()
    result = await orch.read_range(fake_db([row(i) for i in range(5)]), "TEST", "1min",
                                   T, T + 5 * M, as_of=T + 5 * M)
    assert result.status == "partial"
    assert result.coverage_known is False


@pytest.mark.asyncio
async def test_warm_hub_does_not_leak_into_history() -> None:
    """DB-first: прогретая память hub не подменяет и не дополняет ответ из БД."""
    hub = CandleHub()
    orch = CandleHubOrchestrator(hub)
    # в памяти — совсем другие бары (имитация устаревшей или чужой истории)
    hub.seed_1m("TEST", [Candle(T + timedelta(days=1), 1.0, 1.0, 1.0, 1.0, 1)], source="db")
    assert len(hub.series("TEST", "1min")) == 1

    result = await orch.read_range(fake_db([row(0), row(1)]), "TEST", "1min",
                                   T, T + 2 * M, as_of=T + 2 * M, **evidence(2))
    assert [c.ts for c in result.candles] == [T, T + M], "в ответе только БД, память не влилась"
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_empty_db_without_evidence_is_missing_not_no_trading() -> None:
    orch = CandleHubOrchestrator()
    result = await orch.read_range(fake_db([]), "TEST", "1min", T, T + M, as_of=T + M)
    assert result.status == "missing"


@pytest.mark.asyncio
async def test_empty_but_proven_calendar_is_no_trading() -> None:
    orch = CandleHubOrchestrator()
    result = await orch.read_range(fake_db([]), "TEST", "1min", T, T + M, as_of=T + M,
                                   expected_minutes=(), calendar_evidence="weekend-calendar")
    assert result.status == "no_trading"


@pytest.mark.asyncio
async def test_non_bool_full_minutes_is_rejected() -> None:
    orch = CandleHubOrchestrator()
    db = fake_db([])
    with pytest.raises(ValueError, match="require_full_minutes"):
        await orch.read_range(db, "TEST", "1min", T, T + M, as_of=T + M,
                              require_full_minutes="yes")
    assert not db.execute.await_count, "валидация обязана отсекать до обращения к БД"


@pytest.mark.asyncio
async def test_sparse_mode_keeps_closed_buckets_instead_of_dropping_them() -> None:
    """ARCH119/ARCH120: неполный закрытый бакет — не потеря истории.

    strict (по умолчанию) отбрасывает его; require_full_minutes=False сохраняет
    закрытый бакет с числом наблюдённых минут, не досинтезируя пустые.
    """
    orch = CandleHubOrchestrator()
    rows = [row(i) for i in (0, 1, 2, 5, 6, 7, 8, 9)]  # 2 полный 5m-бакет, 1 неполный
    as_of = T + 15 * M

    strict = await orch.read_range(fake_db(rows), "TEST", "5min", T, as_of, as_of=as_of)
    sparse = await orch.read_range(fake_db(rows), "TEST", "5min", T, as_of, as_of=as_of,
                                   require_full_minutes=False)

    assert len(strict.candles) == 1, "strict обязан отбросить неполный бакет"
    assert len(sparse.candles) == 2, "sparse обязан сохранить закрытый неполный бакет"
    assert sparse.sparse_buckets, "sparse-бакет обязан быть помечен явно"
    stamps = [ts for ts, _ in sparse.sparse_buckets]
    counts = [n for _, n in sparse.sparse_buckets]
    assert stamps == [T], "помечен только неполный бакет"
    assert counts == [3], "число наблюдённых минут, без досинтеза"


def test_eviction_is_counted_not_silent() -> None:
    """Обрезка по maxlen считается: глубина памяти не выдаётся за загруженную."""
    hub = CandleHub(maxlen=3)
    bars = [Candle(T + i * M, 100.0, 101.0, 99.0, 100.0, 1) for i in range(8)]
    hub.seed_1m("TEST", bars, source="db")
    series = hub.series("TEST", "1min")
    assert len(series) == 3
    assert series.evicted == 5, "вытеснено ровно столько, сколько не поместилось"
    assert series.last.ts == T + 7 * M, "в памяти остались последние бары"
