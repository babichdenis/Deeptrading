"""Инвариант: ТФ-строки БД == канонический Resampler (START; доказано против T-Invest 102/102).

Нужна БД с данными; иначе skip. Проверяем 10min (interval=8) и hour (interval=4) для SBER.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]

FIGI_SBER = "BBG004730N88"
INTERVALS = {8: 600, 4: 3600}


def _oem():
    spec = importlib.util.spec_from_file_location("ose_exit_matrix", ROOT / "scripts" / "ose_exit_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sync_engine():
    from app.config import get_settings
    return create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)


@pytest.mark.integration
@pytest.mark.parametrize("ival,tf_s", sorted(INTERVALS.items()))
def test_db_tf_matches_resampler(ival: int, tf_s: int):
    eng = _sync_engine()
    with eng.connect() as c:
        f = c.execute(text("SELECT figi FROM instruments WHERE upper(ticker)='SBER' LIMIT 1")).scalar()
        if not f:
            pytest.skip("нет SBER в instruments")
        rows = c.execute(text(
            "SELECT min(ts), max(ts), count(*) FROM candles WHERE figi=:f AND interval=:i"),
            {"f": f, "i": ival}).one()
    if not rows[0]:
        pytest.skip(f"нет ТФ-строк interval={ival} (пересобрать: scripts/rebuild_tf_tables.py)")
    mn, mx, cnt = rows
    oem = _oem()
    _rows, bars = oem._load_tf_cached(f, mn.date().isoformat(), mx.date().isoformat(), tf_s)
    # канонический ряд строится с 00:00 дня mn — сравниваем только пересечение диапазона
    bars = [b for b in bars if mn <= b.ts <= mx]
    with eng.connect() as c:
        db = c.execute(text(
            "SELECT ts, open, high, low, close FROM candles WHERE figi=:f AND interval=:i "
            "AND ts >= :a AND ts <= :b ORDER BY ts"), {"f": f, "i": ival, "a": mn, "b": mx}).all()
    assert len(db) == cnt, "число ТФ-строк изменилось в процессе"
    bar_by_ts = {b.ts: b for b in bars}
    diffs = missing = 0
    # Последняя строка — возможно, ещё формировавшийся на момент пересбора бакет («сейчас»);
    # канон, собранный позже, может содержать больше минут. Сравниваем всё, кроме последней.
    for ts, o, h, low, cl in db[:-1]:
        b = bar_by_ts.get(ts)
        if b is None:
            missing += 1
            continue
        if (float(o), float(h), float(low), float(cl)) != (b.open, b.high, b.low, b.close):
            diffs += 1
    assert missing == 0, f"нет канонических баров для {missing} строк БД"
    assert diffs == 0, f"ТФ БД != Resampler: {diffs} расхождений из {len(db)} (пересобрать ТФ-таблицы)"


SAMPLE_DAYS = 14
# Основные ТФ контуров, покрытые каноническим Resampler (day/week/month — вне его:
# Resampler поддерживает максимум 4h; их при пересборе строила та же floor-логика).
SAMPLE_INTERVALS = {2: 300, 3: 900, 8: 600, 9: 1800, 4: 3600, 10: 7200, 11: 14400}


@pytest.mark.integration
def test_db_tf_sample_matches_resampler():
    """Выборка 5 самых ликвидных фиг × 7 ТФ (5m..4h), окно последних SAMPLE_DAYS дней.

    Последний ТФ-бар каждого интервала может быть формирующимся на момент
    пересбора — исключается, как и в SBER-тесте.
    """
    from datetime import timedelta

    eng = _sync_engine()
    with eng.connect() as c:
        figis = [r[0] for r in c.execute(text(
            "SELECT figi FROM candles WHERE interval=2 AND ts >= now() - interval '30 days' "
            "GROUP BY figi ORDER BY count(*) DESC LIMIT 5")).all()]
    if not figis:
        pytest.skip("нет 5min-строк за последние 30 дней")
    oem = _oem()
    problems: list[str] = []
    checked = 0
    for f in figis:
        with eng.connect() as c:
            mx = c.execute(text(
                "SELECT max(ts) FROM candles WHERE figi=:f AND interval=2"), {"f": f}).scalar()
        if not mx:
            continue
        mn = mx - timedelta(days=SAMPLE_DAYS)
        for ival, tf_s in sorted(SAMPLE_INTERVALS.items()):
            with eng.connect() as c:
                db = c.execute(text(
                    "SELECT ts, open, high, low, close FROM candles WHERE figi=:f AND interval=:i "
                    "AND ts >= :a AND ts <= :b ORDER BY ts"),
                    {"f": f, "i": ival, "a": mn, "b": mx}).all()
            if not db:
                continue
            _rows, bars = oem._load_tf_cached(f, mn.date().isoformat(), mx.date().isoformat(), tf_s)
            bar_by_ts = {b.ts: b for b in bars}
            diffs = missing = 0
            for ts, o, h, low, cl in db[:-1]:
                b = bar_by_ts.get(ts)
                if b is None:
                    missing += 1
                    continue
                if (float(o), float(h), float(low), float(cl)) != (b.open, b.high, b.low, b.close):
                    diffs += 1
            checked += len(db) - 1
            if missing or diffs:
                problems.append(f"{f} interval={ival}: missing={missing} diffs={diffs} of {len(db)}")
    assert checked > 0, "нечего проверять (нет ТФ-строк в окне)"
    assert not problems, "ТФ БД != Resampler (пересобрать): " + "; ".join(problems)
