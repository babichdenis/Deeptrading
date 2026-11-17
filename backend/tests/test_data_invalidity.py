"""Пометка невалидных данных: реестр покрывает ровно то, что испорчено, и не больше.

Смысл тестов: пока история до 24.11.2025 не перекачана, испорченный участок должен
быть помечен, а всё остальное — оставаться чистым. Если пометка «съедет», тесты
скажут об этом раньше, чем на испорченном участке построят бэктест.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import data_invalidity as di

BAD = "high < low OR high < GREATEST(open,close) OR low > LEAST(open,close)"


def _manifest() -> dict:
    assert di.MANIFEST_PATH.exists(), f"нет манифеста {di.MANIFEST_PATH}"
    return json.loads(di.MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_schema():
    blob = _manifest()
    assert blob["ranges"], "манифест пуст"
    assert blob["reason"]
    for r in blob["ranges"]:
        assert r["figi"] and isinstance(r["figi"], str)
        assert isinstance(r["interval"], int) and r["interval"] > 0
        start = datetime.fromisoformat(r["ts_from"])
        end = datetime.fromisoformat(r["ts_to"])
        assert start <= end, f"перевёрнутый диапазон: {r}"
        assert r["bad_bars"] > 0, f"пустая пометка: {r}"


def test_summary_matches_ranges():
    blob = _manifest()
    s = di.summary()
    assert s["pairs"] == len(blob["ranges"])
    assert s["bad_bars"] == sum(int(r["bad_bars"]) for r in blob["ranges"])
    assert s["figis"] == len({r["figi"] for r in blob["ranges"]})


def test_is_invalid_inside_and_outside():
    r = _manifest()["ranges"][0]
    start = datetime.fromisoformat(r["ts_from"])
    end = datetime.fromisoformat(r["ts_to"])
    assert di.is_invalid(r["figi"], start, r["interval"]) is True
    assert di.is_invalid(r["figi"], end, r["interval"]) is True
    assert di.is_invalid(r["figi"], start - timedelta(minutes=1), r["interval"]) is False
    assert di.is_invalid(r["figi"], end + timedelta(minutes=1), r["interval"]) is False
    assert di.is_invalid("НЕТ_ТАКОЙ_БУМАГИ", start, r["interval"]) is False


def test_is_invalid_respects_interval():
    figi = _manifest()["ranges"][0]["figi"]
    assert di.ranges_for(figi, 1)
    assert di.is_invalid(figi, datetime(2025, 10, 6, tzinfo=timezone.utc), 1) is True
    assert di.ranges_for(figi, 9999) == []


def test_overlaps_detects_touching_and_inside():
    r = _manifest()["ranges"][0]
    start = datetime.fromisoformat(r["ts_from"])
    end = datetime.fromisoformat(r["ts_to"])
    inner = start + (end - start) / 2
    assert di.overlaps(r["figi"], start, inner, r["interval"])
    assert di.overlaps(r["figi"], start - timedelta(days=1), start, r["interval"])
    assert di.overlaps(r["figi"], end, end + timedelta(days=1), r["interval"])
    assert not di.overlaps(r["figi"], end + timedelta(days=2), end + timedelta(days=3), r["interval"])


@pytest.mark.integration
def test_every_bad_bar_is_covered_by_manifest():
    """Ни один испорченный бар в базе не остаётся вне пометки."""
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    manifest = _manifest()
    with eng.connect() as c:
        rows = c.execute(text(
            f"SELECT figi, interval, count(*) FROM candles WHERE {BAD} GROUP BY figi, interval")).all()
    assert rows, "в базе нет испорченных баров — пометка больше не нужна?"
    covered = {(r["figi"], int(r["interval"])): r for r in manifest["ranges"]}
    uncovered = []
    for figi, ival, n in rows:
        r = covered.get((figi, int(ival)))
        if r is None:
            uncovered.append((figi, int(ival), n))
            continue
        with eng.connect() as c:
            mn, mx = c.execute(text(
                f"SELECT min(ts), max(ts) FROM candles WHERE figi=:f AND interval=:i AND ({BAD})"),
                {"f": figi, "i": int(ival)}).one()
        start = datetime.fromisoformat(r["ts_from"])
        end = datetime.fromisoformat(r["ts_to"])
        if mn < start or mx > end:
            uncovered.append((figi, int(ival), f"диапазон уже факта: {mn}..{mx} vs {start}..{end}"))
    assert not uncovered, f"вне пометки остались: {uncovered[:5]}"


@pytest.mark.integration
def test_manifest_marks_only_really_bad_pairs():
    """Обратная проверка: каждая пометка указывает на реально испорченные строки."""
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    empty = []
    with eng.connect() as c:
        for r in _manifest()["ranges"]:
            n = c.execute(text(
                f"SELECT count(*) FROM candles WHERE figi=:f AND interval=:i AND {BAD} "
                "AND ts >= :a AND ts <= :b"),
                {"f": r["figi"], "i": int(r["interval"]),
                 "a": r["ts_from"], "b": r["ts_to"]}).scalar()
            if not n:
                empty.append((r["figi"], r["interval"]))
    assert not empty, f"пометки без испорченных строк: {empty[:5]}"


@pytest.mark.integration
def test_recent_tail_is_clean():
    """Свежий хвост (после последней пометки) чист: на нём и строим."""
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        mx = c.execute(text("SELECT max(ts) FROM candles WHERE interval=1")).scalar()
        days = 30
        n = c.execute(text(
            f"SELECT count(*) FROM candles WHERE interval=1 AND {BAD} AND ts >= :a"),
            {"a": mx - timedelta(days=days)}).scalar()
    assert n == 0, f"в последних {days} днях снова появились испорченные бары: {n}"
