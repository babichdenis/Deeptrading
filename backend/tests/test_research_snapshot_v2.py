"""RESEARCH-CORE-V2 (B1): тесты research_snapshot_v2 на изолированной БД.

Герметично против тестовой БД (PIPELINE_TEST_DATABASE_URL, PG14 локальная или
deeptrading_b1_test): time bounds, повторная идентичность, immutable
destination, invalid diagnostics, derived daily с partial-пометкой, universe
строго из файла.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

UTC = timezone.utc
DAY = datetime(2026, 6, 8, tzinfo=UTC)          # Monday
FIGI_T1 = "T1FIGI000000000001"
FIGI_T2 = "T2FIGI000000000002"


def _db_url() -> str:
    url = os.environ.get("PIPELINE_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("нет PIPELINE_TEST_DATABASE_URL")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _ts(hhmm_utc: str) -> datetime:
    h, m = hhmm_utc.split(":")
    return DAY.replace(hour=int(h), minute=int(m))


async def _mk_schema(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id serial PRIMARY KEY, figi varchar(32), interval smallint,
            ts timestamptz, open numeric(20,8), high numeric(20,8),
            low numeric(20,8), close numeric(20,8), volume bigint,
            CONSTRAINT uq UNIQUE (figi, interval, ts))""")


async def _seed(conn):
    await conn.execute("DELETE FROM candles WHERE figi IN ($1, $2)", FIGI_T1, FIGI_T2)
    rows = [
        # валидная сессия 10:00-10:03 MSK (07:00-07:03 UTC) + невалидная строка
        (FIGI_T1, 1, _ts("07:00"), 100, 101, 99, 100.5, 1000),
        (FIGI_T1, 1, _ts("07:01"), 100.5, 102, 100, 101, 1100),
        (FIGI_T1, 1, _ts("07:02"), 101, 101, 98, 100, 900),
        (FIGI_T1, 1, _ts("07:03"), 100, 100.5, 99.5, 100.2, 800),
        (FIGI_T1, 1, _ts("07:04"), 0, 101, 99, 100, 500),      # невалид: open=0
        # строка вне окна экспорта (2026-06-09 07:00)
        (FIGI_T1, 1, _ts("07:00") .replace(day=9), 100, 101, 99, 100, 700),
        # дневная свеча из БД за 2026-06-08
        (FIGI_T1, 24, DAY, 99, 105, 98, 104, 5000),
        # t2: только 1m, без db daily (проверка derived)
        (FIGI_T2, 1, _ts("07:00"), 50, 51, 49, 50.5, 300),
        (FIGI_T2, 1, _ts("07:10"), 50.5, 52, 50, 51, 320),
    ]
    await conn.executemany(
        "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING", rows)


def _universe_file(tmp: Path) -> Path:
    p = tmp / "universe.json"
    p.write_text(json.dumps({
        "n_instruments": 2,
        "instruments": [
            {"ticker": "T1", "figi": FIGI_T1},
            {"ticker": "T2", "figi": FIGI_T2},
        ]}), encoding="utf-8")
    return p


def _run_export(url: str, uni: Path, out: Path,
                start="2026-06-08T00:00:00+00:00", end="2026-06-09T00:00:00+00:00") -> int:
    import research_snapshot_v2 as rs
    args = argparse_ns(start=start, end=end, daily_start=None,
                       universe_file=str(uni), out=str(out), db_url=url, limit=None)
    return asyncio.run(rs.run(args))


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def conn():
    import asyncpg
    url = _db_url()
    loop = asyncio.new_event_loop()
    c = loop.run_until_complete(asyncpg.connect(url))

    def _run(coro):
        return loop.run_until_complete(coro)

    _run(_mk_schema(c))
    _run(_seed(c))
    c._loop = loop
    yield c
    _run(c.close())
    loop.close()


def test_time_bounds_and_invalid_rows(conn, tmp_path):
    url = _db_url()
    out = tmp_path / "snap1"
    assert _run_export(url, _universe_file(tmp_path), out) == 0
    csv1 = out / "csv" / "T1__m1.csv"
    rows = list(csv.DictReader(csv1.open()))
    ts_list = [r["ts"] for r in rows]
    # 4 валидные строки в окне; невалидная (open=0) исключена; внеоконная исключена
    assert len(rows) == 4
    assert all(t < "2026-06-09" for t in ts_list)
    manifest = json.loads((out / "manifest.json").read_text())
    m1 = manifest["per_ticker"]["T1"]["m1"]
    assert m1["written_rows"] == 4 and m1["invalid_rows"] == 1
    inv = list(csv.DictReader((out / "invalid_rows.csv").open()))
    assert inv and inv[0]["reason"] == "nonpositive_open"


def test_repeat_run_identical_csv(conn, tmp_path):
    url = _db_url()
    uni = _universe_file(tmp_path)
    out1, out2 = tmp_path / "snap_a", tmp_path / "snap_b"
    assert _run_export(url, uni, out1) == 0
    assert _run_export(url, uni, out2) == 0
    for f in ("T1__m1.csv", "T1__m24.csv", "T2__m1.csv"):
        assert _sha(out1 / "csv" / f) == _sha(out2 / "csv" / f), f


def test_immutable_destination_refuses_nonempty(conn, tmp_path):
    url = _db_url()
    out = tmp_path / "snap_r"
    assert _run_export(url, _universe_file(tmp_path), out) == 0
    assert _run_export(url, _universe_file(tmp_path), out) == 2


def test_derived_daily_and_partial_marking(conn, tmp_path):
    url = _db_url()
    out = tmp_path / "snap_d"
    assert _run_export(url, _universe_file(tmp_path), out) == 0
    # у T1 db daily есть → derived пустой
    der1 = out / "csv" / "T1__d1_derived.csv"
    assert len(list(csv.DictReader(der1.open()))) == 0
    # у T2 db daily нет → derived из 1m, partial (сессия не полная)
    der2 = out / "csv" / "T2__d1_derived.csv"
    rows2 = list(csv.DictReader(der2.open()))
    assert len(rows2) == 1
    assert rows2[0]["source"] == "derived_from_1m"
    assert rows2[0]["partial"] == "partial_session"


def test_universe_strictly_from_file(conn, tmp_path):
    url = _db_url()
    uni = _universe_file(tmp_path)
    # в БД заведомо больше тикеров, чем в universe-файле
    out = tmp_path / "snap_u"
    assert _run_export(url, uni, out) == 0
    files = sorted(p.name for p in (out / "csv").glob("*.csv"))
    assert set(files) == {"T1__m1.csv", "T1__m24.csv", "T2__m1.csv",
                          "T2__m24.csv", "T1__d1_derived.csv", "T2__d1_derived.csv"}
