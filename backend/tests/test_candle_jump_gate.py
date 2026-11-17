"""Golden-проверки отсева баров с ценой чужого ряда (validate_jumps).

Случай из базы .8: 22.08.2026 в FIGI MVID с 13:08 до 21:59 лежали цены 4212–4239
(диапазон LKOH) при нормальных 44.50–46.30 до и после. Бар структурно корректен,
поэтому проверка строки его пропускает — отсечь может только сверка с соседом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import candle_ingestion_contract as cic

UTC = timezone.utc


def _row(figi: str, ts: datetime, close: float, interval: int = 1) -> dict:
    return {
        "figi": figi, "interval": interval, "ts": ts,
        "open": close, "high": close, "low": close, "close": close, "volume": 10,
    }


def _series(figi: str, closes: list[float], start: str = "2026-08-22T10:00:00") -> list[dict]:
    t0 = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return [_row(figi, t0 + timedelta(minutes=i), c) for i, c in enumerate(closes)]


def test_foreign_price_rejected() -> None:
    rows = _series("MVID", [46.05, 46.10, 4239.00, 46.20])
    kept, rejected = cic.validate_jumps(rows, {})
    assert [r["close"] for r in kept] == [46.05, 46.10, 46.20]
    assert len(rejected) == 1
    assert rejected[0].reason == cic.JUMP_TOO_BIG


def test_big_gap_within_threshold_kept() -> None:
    rows = _series("SBER", [300.0, 345.0])  # +15% дивидендный гэп
    kept, rejected = cic.validate_jumps(rows, {}, jump_thr=0.40)
    assert len(kept) == 2
    assert rejected == []


def test_anchor_catches_first_bar() -> None:
    rows = _series("SBER", [304.0, 304.5])
    kept, rejected = cic.validate_jumps(rows, {("SBER", 1): 72.8})
    assert kept == []
    assert len(rejected) == 2


def test_rejected_bar_is_not_a_reference() -> None:
    # битый бар не должен «узаконить» второй битый бар
    rows = _series("X", [100.0, 5000.0, 5001.0, 100.2])
    kept, rejected = cic.validate_jumps(rows, {})
    assert [r["close"] for r in kept] == [100.0, 100.2]
    assert len(rejected) == 2


def test_gate_can_be_disabled() -> None:
    rows = _series("X", [100.0, 5000.0])
    kept, rejected = cic.validate_jumps(rows, {}, jump_thr=0.0)
    assert len(kept) == 2
    assert rejected == []


def test_split_requires_explicit_anchor_reset() -> None:
    """Сплит автоматически от чужого ряда не отличается — известное ограничение.

    После смены уровня (2388 -> 249) все последующие бары сравниваются со старым
    закрытием и отсеиваются, пока опора не сброшена. Для корпоративных событий это
    лечится перезаливом дня с явным якорем (scripts/refill_candle_days.py, гейт off).
    Безопасное поведение для бота: торговать на неподтверждённом уровне не начинаем.
    """
    rows = _series("T", [2388.0, 249.0, 249.5, 250.0])
    kept, rejected = cic.validate_jumps(rows, {}, jump_thr=0.40)
    assert [r["close"] for r in kept] == [2388.0]
    assert len(rejected) == 3

    # после сброса опоры на новый уровень ряд принимается
    kept2, rejected2 = cic.validate_jumps(rows, {("T", 1): 249.5}, jump_thr=0.40)
    assert [r["close"] for r in kept2] == [249.0, 249.5, 250.0]
    assert len(rejected2) == 1  # 2388 несовместим с новой опорой
