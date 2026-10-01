"""AUDIT P1.2: реальный lot_size пробрасывается в выдачу Universe.

Hardcode `lot_size: 10` в select_screened_universe был не косметическим: в БД
лоты от 1 до 1 000 000, и для 10-шаговых инструментов размер позиции менялся на
три порядка. Тесты чисто функциональные — реальный SQL проверяется ночью
против живой БД.
"""
from __future__ import annotations

import inspect

import pytest

from app.bot.universe.discovery import DEFAULT_LOT, fetch_lot_by_figi
from app.bot.universe.runtime_select import UNIVERSE_MODES, select_screened_universe


def test_default_lot_is_ten():
    """Константа объявлена явно и равна прежнему хардкоду — это осознанный fallback."""
    assert DEFAULT_LOT == 10


@pytest.mark.parametrize("mode", UNIVERSE_MODES)
def test_no_hardcoded_lot_size_in_source(mode):
    """В runtime_select.py не должно быть литерала lot_size: 10 (AUDIT P1.2)."""
    src = inspect.getsource(__import__(
        "app.bot.universe.runtime_select", fromlist=["select_screened_universe"]
    ))
    assert '"lot_size": 10' not in src
    assert '"lot_size": 10,' not in src


def test_select_screened_universe_passes_lot_through():
    """В выдаче lot_size обязан приходить из словаря лотов, а не из константы.

    Проверяем на уровне исходника выдачи: значение собирается выражением
    lot_by_figi.get(figi, DEFAULT_LOT), то есть известный лот проходит как есть,
    а неизвестный явно помечается DEFAULT_LOT (см. тест фолбэка ниже).
    """
    src = inspect.getsource(select_screened_universe)
    assert "lot_by_figi.get(" in src
    assert "DEFAULT_LOT" in src


def test_fetch_lot_signature_uses_instrument_info():
    """Лот берётся из instrument_info — единственного источника лота в проекте."""
    src = inspect.getsource(fetch_lot_by_figi)
    assert "instrument_info" in src
    assert "lot" in src


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Мини-драйвер: отдаёт заранее заданные строки instrument_info."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _FakeResult(self.rows)


async def _run_fetch(rows, figis):
    db = _FakeDB(rows)
    return await fetch_lot_by_figi(db, figis), db


def test_fetch_lot_reads_rows():
    import asyncio

    lot, db = asyncio.run(
        _run_fetch([("F1", 1), ("F2", 100)], ["F1", "F2"])
    )
    assert lot == {"F1": 1, "F2": 100}
    assert "instrument_info" in db.calls[0][0]


def test_fetch_lot_preserves_large_lots():
    """Крупные лоты не должны схлопываться в DEFAULT_LOT — это и был баг."""
    import asyncio

    rows = [("F1", 1), ("F2", 1000), ("F3", 100000), ("F4", 1000000)]
    lot, _ = asyncio.run(_run_fetch(rows, ["F1", "F2", "F3", "F4"]))
    assert lot == {"F1": 1, "F2": 1000, "F3": 100000, "F4": 1000000}


def test_fetch_lot_null_lot_falls_back_to_default():
    """NULL/0 лот — это DEFAULT_LOT, а не 0: нулевой лот нельзя передать на биржу."""
    import asyncio

    lot, _ = asyncio.run(_run_fetch([("F1", None), ("F2", 0), ("F3", 5)], ["F1", "F2", "F3"]))
    assert lot == {"F1": DEFAULT_LOT, "F2": DEFAULT_LOT, "F3": 5}


def test_fetch_lot_unknown_figi_absent_from_result():
    """FIGI без строки в instrument_info отсутствует: решение принимает вызывающий."""
    import asyncio

    lot, _ = asyncio.run(_run_fetch([("F1", 7)], ["F1", "F_MISSING"]))
    assert lot == {"F1": 7}
    assert "F_MISSING" not in lot


def test_fetch_lot_dedups_and_strips():
    import asyncio

    db = _FakeDB([("F1", 3)])
    lot = asyncio.run(fetch_lot_by_figi(db, ["F1", "F1", "  "]))
    assert lot == {"F1": 3}
    params = db.calls[0][1]
    assert params["figis"] == ["F1"]


def test_fetch_lot_empty_input_no_db_call():
    import asyncio

    db = _FakeDB([("F1", 3)])
    assert asyncio.run(fetch_lot_by_figi(db, [])) == {}
    assert asyncio.run(fetch_lot_by_figi(db, ["", "  "])) == {}
    assert db.calls == []


def test_fetch_lot_skips_null_figi_in_rows():
    import asyncio

    lot, _ = asyncio.run(_run_fetch([(None, 5), ("F1", 2)], ["F1"]))
    assert lot == {"F1": 2}
