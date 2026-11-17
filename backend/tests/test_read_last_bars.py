"""read_last_bars — «последние n закрытых баров» через тот же read_range.

Потребителям (отбор бумаг, лаборатория, телеметрия) нужен не календарный
диапазон, а фиксированное число баров. Чтобы они не писали свой SQL и не
заводили второй способ чтения свечей, добавлен этот метод: он только читает,
расширяет окно шагами (ночи/выходные дают разрывы) и возвращает HistoricalRange,
где видно фактическое покрытие.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.services.candle_hub_orchestrator import CandleHubOrchestrator
from app.services.candle_hub_history import HistoricalRange

AS_OF = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)


def _range(candles: list[datetime], *, status: str = "partial") -> HistoricalRange:
    from app.engine.models import Candle

    return HistoricalRange(
        candles=tuple(
            Candle(ts=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0)
            for ts in candles),
        status=status,
        coverage_known=False,
        source_rows=len(candles),
        missing_minutes=(),
        unexpected_minutes=(),
        incomplete_buckets=(),
        calendar_evidence=None,
    )


def _series(n: int, step_min: int = 5) -> list[datetime]:
    base = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    return [base + timedelta(minutes=step_min * i) for i in range(n)]


def _orch_with(calls: list[tuple], results: list[HistoricalRange]) -> CandleHubOrchestrator:
    orch = CandleHubOrchestrator()

    async def fake_read_range(db, figi, tf, start, end, *, as_of, **kw):
        calls.append((figi, tf, start, end, as_of))
        idx = min(len(calls) - 1, len(results) - 1)
        return results[idx]

    orch.read_range = fake_read_range  # type: ignore[method-assign]
    return orch


@pytest.mark.asyncio
async def test_returns_n_bars_without_extra_calls() -> None:
    calls: list[tuple] = []
    orch = _orch_with(calls, [_range(_series(6))])
    res = await orch.read_last_bars(None, "SBER", "5min", 5, as_of=AS_OF)
    assert len(res.candles) == 5
    assert len(calls) == 1, "баров хватило с первого окна — расширять нечего"


@pytest.mark.asyncio
async def test_expands_window_when_not_enough_bars() -> None:
    calls: list[tuple] = []
    orch = _orch_with(calls, [_range(_series(2)), _range(_series(7))])
    res = await orch.read_last_bars(None, "SBER", "5min", 5, as_of=AS_OF)
    assert len(res.candles) == 5
    assert len(calls) == 2, "второго окна достаточно"
    span1 = calls[0][4] - calls[0][2]
    span2 = calls[1][4] - calls[1][2]
    assert span2 > span1, "окно обязано расширяться назад"


@pytest.mark.asyncio
async def test_gives_up_after_max_expansions() -> None:
    calls: list[tuple] = []
    orch = _orch_with(calls, [_range(_series(1))])
    res = await orch.read_last_bars(None, "SBER", "day", 5, as_of=AS_OF, max_expansions=2)
    assert len(res.candles) == 1
    assert len(calls) == 3, "первое окно плюс два расширения"
    assert res.status == "partial", "недобор честно виден в статусе"


@pytest.mark.asyncio
async def test_keeps_last_bars_in_order() -> None:
    calls: list[tuple] = []
    full = _series(9)
    orch = _orch_with(calls, [_range(full)])
    res = await orch.read_last_bars(None, "SBER", "5min", 4, as_of=AS_OF)
    assert [c.ts for c in res.candles] == full[-4:]


@pytest.mark.asyncio
async def test_rejects_bad_n_and_naive_as_of() -> None:
    orch = CandleHubOrchestrator()
    with pytest.raises(ValueError):
        await orch.read_last_bars(None, "SBER", "5min", 0, as_of=AS_OF)
    with pytest.raises(ValueError):
        await orch.read_last_bars(None, "SBER", "5min", 5, as_of=datetime(2026, 11, 15, 16, 0))


@pytest.mark.asyncio
async def test_reads_only_no_writes_no_sdk() -> None:
    """Контракт ARCH107: чтение не пишет и не ходит в источник."""
    src = inspect.getsource(CandleHubOrchestrator.read_last_bars)
    for forbidden in ("upsert_candles", "fetch_candles", "fill_gaps", "db.commit",
                      "db.execute", "select("):
        assert forbidden not in src, f"чтение не должно делать {forbidden}"
