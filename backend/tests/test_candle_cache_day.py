"""Ресэмплинг дневного ряда: код интервала и согласие с минутами.

День внутри проекта хранится под кодом 24 (tf_freshness, move_select), провайдер
отдаёт код 5. Раньше ресэмплинг писал под провайдерским кодом, и дневной ряд для
бумаг без старой истории просто не появлялся: сверка свежести искала код 24.

Тест идёт в изолированной тестовой БД (PIPELINE_TEST_DATABASE_URL): ресэмплинг
чистит день запросом с синтаксисом PostgreSQL, поэтому sqlite здесь не подходит.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models.candle import Candle
from app.services.candle_cache import CANONICAL_DAY_INTERVAL, resample_from_1m
from tests.fixtures.pipeline_contract.db import (
    ENV_VAR,
    make_sessionmaker,
    prepare_schema,
    resolve_test_target,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

FIGI = "TESTDAY00001"
DAY = datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc)  # 10:00 МСК


def _has_contour() -> bool:
    return bool((os.environ.get(ENV_VAR) or "").strip())


@pytest.fixture
async def db():
    if not _has_contour():
        pytest.skip(f"дневной ресэмплинг: задать {ENV_VAR} (test-БД)")
    url = resolve_test_target().url
    await prepare_schema(url)
    engine, maker = make_sessionmaker(url)
    try:
        async with maker() as session:
            await session.execute(text("TRUNCATE TABLE candles RESTART IDENTITY"))
            for i, (o, h, l, c) in enumerate([(100, 101, 99, 100), (100, 105, 100, 104),
                                              (104, 104, 98, 99)]):
                session.add(Candle(figi=FIGI, interval=1, ts=DAY + timedelta(minutes=i),
                                   open=o, high=h, low=l, close=c, volume=10))
            await session.commit()
            yield session
    finally:
        await engine.dispose()


async def _day_bar(session):
    rows = (await session.execute(
        text("SELECT ts, open, high, low, close, volume FROM candles "
             "WHERE figi = :f AND interval = :i"), {"f": FIGI, "i": CANONICAL_DAY_INTERVAL}
    )).all()
    return rows


async def test_day_goes_to_canonical_code(db):
    written = await resample_from_1m(db, FIGI, "day", DAY, DAY + timedelta(days=1))
    await db.commit()

    assert written == 1
    rows = await _day_bar(db)
    assert len(rows) == 1, "дневной бар должен лежать под каноническим кодом 24"
    _, open_, high, low, close, volume = rows[0]
    assert (open_, high, low, close) == (100, 105, 98, 99)
    assert int(volume) == 30


async def test_day_label_is_utc_midnight(db):
    await resample_from_1m(db, FIGI, "day", DAY, DAY + timedelta(days=1))
    await db.commit()

    rows = await _day_bar(db)
    assert len(rows) == 1
    ts = rows[0][0]
    assert ts.astimezone(timezone.utc) == datetime(2026, 11, 13, tzinfo=timezone.utc)


async def test_day_matches_minutes(db):
    await resample_from_1m(db, FIGI, "day", DAY, DAY + timedelta(days=1))
    await db.commit()

    minutes = (await db.execute(
        text("SELECT open, high, low, close FROM candles WHERE figi = :f AND interval = 1 "
             "ORDER BY ts"), {"f": FIGI})).all()
    rows = await _day_bar(db)
    assert len(rows) == 1
    _, open_, high, low, close, _v = rows[0]
    assert open_ == minutes[0][0]
    assert close == minutes[-1][3]
    assert high == max(m[1] for m in minutes)
    assert low == min(m[2] for m in minutes)
