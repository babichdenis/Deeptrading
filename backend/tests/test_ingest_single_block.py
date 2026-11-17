"""Вход свечей и индексов — одна точка: candlehub.

Требование владельца: всё, что касается входа свечей и индексов, решает ОДИН
блок (candlehub), а не каждый модуль по-своему. Проверяем:

1) политика нормализации живёт в candlehub и работает на неизменяемой Candle;
2) контракт приёма (cic.normalize_ohlc) делегирует в хаб, а не держит копию;
3) блок записи в БД (upsert_candles_detailed) нормализует ДО валидации;
4) индексный путь MOEX ISS тоже идёт через политику хаба.

Плюс поведение само по себе: битый диапазон расширяется до согласованного вида,
а законные бары (нулевой объём, честно плоский) не трогаются.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

from app.bot import moex
from app.engine.candlehub import (
    Candle,
    normalize_candle_for_ingest,
    normalize_row_for_ingest,
    validate_candle,
)
from app.services import candle_ingestion_contract as cic
from app.services import tinvest


def _candle(**kw) -> Candle:
    base = dict(
        ts=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0,
    )
    base.update(kw)
    return Candle(**base)


def _row(**kw) -> dict:
    base = {
        "figi": "TEST", "interval": 1,
        "ts": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------ поведение политики
def test_hub_accepts_consistent_candle_untouched() -> None:
    c = _candle()
    fixed, changed = normalize_candle_for_ingest(c)
    assert changed is False
    assert fixed is c


def test_hub_expands_flat_bar_with_body_outside() -> None:
    """Исторический случай: high == low, а тело выше. Так пришли 4.5 млн строк."""
    c = _candle(open=102.0, high=101.0, low=101.0, close=102.0)
    assert validate_candle(c) == "bad_range", "исходный бар обязан быть отвергнут"
    fixed, changed = normalize_candle_for_ingest(c)
    assert changed is True
    assert fixed.high == 102.0 and fixed.low == 101.0
    assert validate_candle(fixed) is None


def test_hub_expands_when_close_below_low() -> None:
    c = _candle(open=100.0, high=100.0, low=100.0, close=99.5)
    fixed, changed = normalize_candle_for_ingest(c)
    assert changed is True
    assert fixed.low == 99.5 and fixed.high == 100.0
    assert validate_candle(fixed) is None


def test_hub_keeps_zero_volume_and_flat_bar() -> None:
    """Нулевой объём и o==h==l==c — законные данные, политика их не трогает."""
    flat = _candle(open=100.0, high=100.0, low=100.0, close=100.0, volume=0.0)
    fixed, changed = normalize_candle_for_ingest(flat)
    assert changed is False and fixed is flat


def test_hub_does_not_mutate_frozen_candle() -> None:
    c = _candle(open=102.0, high=101.0, low=101.0, close=102.0)
    normalize_candle_for_ingest(c)
    assert (c.high, c.low) == (101.0, 101.0), "исходная Candle неизменяема"


def test_hub_row_policy_leaves_source_dict_intact() -> None:
    row = _row(open=102.0, high=101.0, low=101.0, close=102.0)
    fixed, changed = normalize_row_for_ingest(row)
    assert changed is True
    assert row["high"] == 101.0, "исходный словарь не меняется"
    assert (fixed["high"], fixed["low"]) == (102.0, 101.0)


def test_hub_rejects_garbage_row_without_raising() -> None:
    fixed, changed = normalize_row_for_ingest({"figi": "X"})
    assert changed is False


def test_persist_contract_rejects_body_outside_range() -> None:
    """Persist-путь (cic.validate_row) отвергает такой бар — отсюда и дыра без входа хаба."""
    rej = cic.validate_row(_row(open=102.0, high=101.0, low=101.0, close=102.0))
    assert rej is not None and rej.reason == cic.BAD_RANGE
    fixed, _ = normalize_row_for_ingest(_row(open=102.0, high=101.0, low=101.0, close=102.0))
    assert cic.validate_row(fixed) is None, "после политики хаба бар проходит контракт"


# --------------------------------------------------- одна точка, а не копии
def test_contract_normalize_delegates_to_hub() -> None:
    src = inspect.getsource(cic.normalize_ohlc)
    assert "normalize_row_for_ingest" in src, (
        "контракт обязан делегировать в candlehub, а не держать свою копию политики")


def test_ingest_normalizes_before_validation() -> None:
    src = inspect.getsource(tinvest.upsert_candles_detailed)
    pos_norm = src.find("normalize_row_for_ingest")
    pos_valid = src.find("assert_valid_batch")
    assert pos_norm != -1, "блок записи обязан звать политику хаба"
    assert pos_valid != -1
    assert pos_norm < pos_valid, (
        "нормализация должна идти ДО валидации, иначе битый бар отвергается, "
        "а не исправляется")


def test_indices_use_hub_policy() -> None:
    src = inspect.getsource(moex._upsert_imoex)
    assert "normalize_row_for_ingest" in src, (
        "путь индексов MOEX ISS обязан идти через политику хаба")


def test_only_hub_owns_the_policy() -> None:
    """В других модулях не должно оставаться собственной арифметики огибающей."""
    hub_src = inspect.getsource(normalize_candle_for_ingest)
    assert "max(h, o, cl, lo)" in hub_src
    contract_src = inspect.getsource(cic.normalize_ohlc)
    assert "max(" not in contract_src.replace("max(1.0", ""), (
        "в контракте не должно быть своей формулы — только делегирование")
