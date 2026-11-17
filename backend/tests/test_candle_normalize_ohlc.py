"""Нормализация бара: максимум не ниже open/close, минимум не выше.

Индексы MOEX ISS иногда отдают бар, у которого закрытие выходит за максимум на доли
пункта. Такой бар пишется мимо контракта (raw INSERT в загрузчике индекса) и ломает
проверку целостности и агрегаты старших ТФ. Бар не выбрасывается — иначе в ряду
появится дыра, — а расширяется до согласованного вида.
"""
from __future__ import annotations

from app.services.candle_ingestion_contract import normalize_ohlc, validate_row


def _row(**kw) -> dict:
    from datetime import datetime, timezone

    base = {"figi": "BBG00KDWPPW2", "interval": 1,
            "ts": datetime(2026, 11, 15, 13, 58, tzinfo=timezone.utc),
            "open": 2512.57, "high": 2512.58, "low": 2512.55, "close": 2512.62,
            "volume": 0}
    base.update(kw)
    return base


def test_close_above_high_is_fixed():
    fixed, changed = normalize_ohlc(_row())
    assert changed is True
    assert fixed["high"] == 2512.62, "максимум обязан догнать закрытие"
    assert fixed["low"] == 2512.55


def test_open_below_low_is_fixed():
    fixed, changed = normalize_ohlc(_row(open=2512.50, close=2512.56))
    assert changed is True
    assert fixed["low"] == 2512.50


def test_consistent_bar_is_untouched():
    row = _row(close=2512.57)
    fixed, changed = normalize_ohlc(row)
    assert changed is False
    assert fixed == row


def test_original_row_is_not_mutated():
    row = _row()
    normalize_ohlc(row)
    assert row["high"] == 2512.58, "исходный словарь меняться не должен"


def test_normalized_row_passes_contract():
    """После расширения бар проходит контрактную проверку — это и есть цель."""
    assert validate_row(_row()) is not None, "до нормализации бар нарушает контракт"
    fixed, _ = normalize_ohlc(_row())
    assert validate_row(fixed) is None


def test_missing_fields_are_returned_as_is():
    row = {"figi": "X", "interval": 1}
    fixed, changed = normalize_ohlc(row)
    assert changed is False and fixed == row


def test_high_below_low_is_fixed():
    fixed, changed = normalize_ohlc(_row(high=2512.40, low=2512.60, open=2512.50, close=2512.55))
    assert changed is True
    assert fixed["high"] == 2512.60 and fixed["low"] == 2512.40
