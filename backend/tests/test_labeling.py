"""Тесты разметки: triple-barrier и zigzag-«идеальные сделки» (ручные числа)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.labeling import (
    BarrierConfig,
    ZigzagConfig,
    triple_barrier,
    zigzag_trades,
)
from app.engine.models import Candle

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def _bar(i, close, half_range=0.5, high=None, low=None):
    h = high if high is not None else close + half_range
    lo = low if low is not None else close - half_range
    return Candle(ts=T0 + timedelta(minutes=10 * i), open=close, high=h, low=lo, close=close)


def _flat(n, close=100.0, hr=0.5, start=0):
    return [_bar(start + i, close, hr) for i in range(n)]


CFG = BarrierConfig(take_atr=1.5, stop_atr=1.0, max_bars=5, atr_period=14)


def _row(rows, index):
    return next(r for r in rows if r["index"] == index)


def test_flat_atr_is_one():
    # sanity: на флэте ±0.5 TR=1.0 → пороги upper=101.5 / lower=99.0
    rows = triple_barrier(_flat(20), CFG)
    assert _row(rows, 19)["upper"] == pytest.approx(101.5)
    assert _row(rows, 19)["lower"] == pytest.approx(99.0)


def test_upper_barrier_hit():
    bars = _flat(30) + [_bar(30, 100.0, high=102.0, low=99.6)]
    r = _row(triple_barrier(bars, CFG), 29)
    assert r["label"] == 1 and r["t1_bars"] == 1 and r["mature"]
    assert r["ret_atr"] == pytest.approx(1.5)


def test_lower_barrier_hit():
    bars = _flat(30) + [_bar(30, 100.0, high=100.3, low=98.0)]
    r = _row(triple_barrier(bars, CFG), 29)
    assert r["label"] == -1 and r["t1_bars"] == 1 and r["mature"]
    assert r["ret_atr"] == pytest.approx(-1.0)


def test_same_bar_conflict_stop_first():
    bars = _flat(30) + [_bar(30, 100.0, high=102.0, low=98.0)]
    r = _row(triple_barrier(bars, CFG), 29)
    assert r["label"] == -1 and r["ret_atr"] == pytest.approx(-1.0)


def test_time_barrier_and_maturity():
    bars = _flat(20, hr=0.2)  # ATR=0.4: upper=100.6, lower=99.6 — не задеваются
    rows = triple_barrier(bars, CFG)
    r14 = _row(rows, 14)  # горизонт 19 <= n-1 -> зрелая
    r19 = _row(rows, 19)  # горизонт 24 > n-1 -> незрелая
    assert r14["label"] == 0 and r14["mature"]
    assert r19["label"] == 0 and not r19["mature"]


def test_zigzag_ideal_trades():
    bars = _flat(15)  # ATR=1.0 c индекса 13
    # впадина подтверждается только отскоком — в хвосте даём рост 98→99
    closes = [99, 98, 97, 98, 99, 100, 101, 102, 101, 100, 99, 98, 97, 98, 99]
    bars += [_bar(15 + i, float(c)) for i, c in enumerate(closes)]
    trades = zigzag_trades(bars, ZigzagConfig(min_move_atr=2.0, atr_period=14))
    assert [t["side"] for t in trades] == ["SHORT", "LONG", "SHORT"]
    assert trades[0]["entry_px"] == pytest.approx(100.5)
    assert trades[0]["exit_px"] == pytest.approx(96.5)
    assert trades[1]["entry_px"] == pytest.approx(96.5)
    assert trades[1]["exit_px"] == pytest.approx(102.5)
    assert trades[2]["entry_px"] == pytest.approx(102.5)
    assert trades[2]["exit_px"] == pytest.approx(96.5)
    assert all(t["bars"] > 0 for t in trades)


def test_zigzag_wide_bar_no_duplicate_entry_ts():
    """Широкий бар (H и L на одном индексе) не даёт две сделки с одним entry_ts."""
    bars = _flat(15)  # ATR=1.0, флэт 99.5..100.5
    bars.append(_bar(15, 101.0, high=102.5, low=99.5))
    bars.append(_bar(16, 100.5, high=101.0, low=100.0))
    bars.append(_bar(17, 101.0, high=101.5, low=100.5))
    bars += _flat(5, start=18)
    trades = zigzag_trades(bars, ZigzagConfig(min_move_atr=2.0, atr_period=14))
    entries = [t["entry_ts"] for t in trades]
    assert len(entries) == len(set(entries)), f"дубли entry_ts: {entries}"
    assert all(t["bars"] > 0 for t in trades)
