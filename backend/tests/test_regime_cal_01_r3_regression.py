"""REGIME-CAL-01 r3: regression-фикстуры на два дефекта r2 из ARCH-88.

Первая ловит потерю поздних входов после незавершённой позиции.
Вторая ловит прогрев по прошедшим часам вместо закрытых баров.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts" / "research"))

import pytest

from app.engine.models import Candle as EngineCandle
import regime_cal_01_r3 as r3

BASE = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def bar(idx: int, o: float, h: float, l: float, c: float) -> EngineCandle:
    return EngineCandle(ts=BASE + timedelta(hours=idx), open=o, high=h, low=l,
                        close=c, volume=100.0)


def build_bars() -> list[EngineCandle]:
    """Флэт, пробой при широком ATR, затем узкий второй вход, который закрывается."""
    bars: list[EngineCandle] = []
    for i in range(25):
        bars.append(bar(i, 100.0, 100.5, 99.5, 100.0))
    bars.append(bar(25, 100.0, 102.5, 99.8, 102.0))
    for i in range(26, 40):
        bars.append(bar(i, 100.0, 100.5, 99.5, 100.0))
    bars.append(bar(40, 100.0, 103.5, 99.9, 103.0))
    bars.append(bar(41, 103.0, 103.5, 102.5, 103.0))
    bars.append(bar(42, 102.8, 103.0, 101.5, 101.8))
    for i in range(43, 48):
        bars.append(bar(i, 101.8, 102.0, 101.0, 101.5))
    return bars


def wide_then_narrow_atr(bars):
    return [10.0 if i < 30 else 0.5 for i in range(len(bars))]


def test_incomplete_does_not_swallow_later_entries(monkeypatch) -> None:
    """Первый вход при ATR 10 остаётся открытым, второй при ATR 0.5 закрывается."""
    bars = build_bars()
    monkeypatch.setattr(r3, "atr_series", wide_then_narrow_atr)
    trades = r3.simulate(bars, labels=[], allowed=None,
                         warmup_close_ts=bars[0].ts, costs=r3.CostsCompat())
    statuses = [t["status"] for t in trades]
    assert "incomplete" in statuses, "первый вход должен остаться незавершённым"
    assert "closed" in statuses, "поздний вход не должен теряться из-за ранней незавершённости"
    closed = [t for t in trades if t["status"] == "closed"][0]
    assert closed["reason"] == "sl_touch"
    assert closed["exit_base"] == pytest.approx(102.0)


def test_old_behaviour_would_lose_the_late_entry(monkeypatch) -> None:
    """Проверка самой фикстуры: ранний вход действительно незавершён до конца данных."""
    bars = build_bars()
    monkeypatch.setattr(r3, "atr_series", wide_then_narrow_atr)
    trades = r3.simulate(bars, labels=[], allowed=None,
                         warmup_close_ts=bars[0].ts, costs=r3.CostsCompat())
    first = trades[0]
    assert first["status"] == "incomplete"
    assert first["reason"] == "data_ended_open_position"
    assert len(trades) > 1, "без исправления здесь была бы одна запись"


def test_warmup_requires_closed_h1_bars_not_elapsed_hours() -> None:
    few = [bar(i * 24, 100.0, 100.5, 99.5, 100.0) for i in range(150)]
    assert r3.warmup_close_from_bars(few) is None, "150 баров меньше требуемых 200"
    start = BASE
    old_style = start + timedelta(hours=r3.WARMUP_H1_BARS)
    assert old_style < few[-1].ts, "по старой логике времени прогрев уже прошёл бы"


def test_warmup_close_is_close_of_required_bar() -> None:
    enough = [bar(i, 100.0, 100.5, 99.5, 100.0) for i in range(250)]
    got = r3.warmup_close_from_bars(enough)
    assert got == enough[r3.WARMUP_H1_BARS - 1].ts + timedelta(seconds=r3.TF_GATE)


def test_no_entries_before_warmup_completes(monkeypatch) -> None:
    bars = build_bars()
    monkeypatch.setattr(r3, "atr_series", wide_then_narrow_atr)
    trades = r3.simulate(bars, labels=[], allowed=None,
                         warmup_close_ts=None, costs=r3.CostsCompat())
    assert trades == [], "при нехватке часовых баров входов быть не должно"


def test_summary_still_has_no_capital_claims() -> None:
    s = r3.summarize([])
    for forbidden in ("equity", "dd", "drawdown"):
        assert forbidden not in s
    assert s["net_rub_sum"] is None
