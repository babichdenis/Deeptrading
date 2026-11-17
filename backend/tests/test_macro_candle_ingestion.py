import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.macro_candle_ingestion import sync_macro_series
from app.services.macro_candle_source import CNYRUB, read_closed_macro_candles


START = datetime(2026, 10, 9, 7, tzinfo=timezone.utc)
COLUMNS = ["begin", "open", "high", "low", "close", "volume"]


def page(*begins):
    return {"candles": {"columns": COLUMNS, "data": [
        [begin, 11, 11, 11, 11, 1] for begin in begins
    ]}}


def test_failed_second_page_does_not_reach_writer():
    def fetch(instrument, start, end):
        def read_json(url):
            if "start=0" in url:
                return page("2026-10-09 10:00:00")
            raise TimeoutError("second ISS page")

        return read_closed_macro_candles(instrument, start, end, read_json=read_json)

    persist = AsyncMock()
    with pytest.raises(TimeoutError, match="second ISS page"):
        asyncio.run(sync_macro_series(None, CNYRUB, START, START + timedelta(minutes=1),
                                      fetch=fetch, persist=persist))
    persist.assert_not_awaited()


def test_open_minute_does_not_reach_writer():
    def fetch(instrument, start, end):
        pages = iter([page("2026-10-09 10:00:00", "2026-10-09 10:01:00"), page()])
        return read_closed_macro_candles(instrument, start, end, read_json=lambda _: next(pages))

    persist = AsyncMock()
    end = START + timedelta(seconds=90)
    asyncio.run(sync_macro_series(None, CNYRUB, START, end, fetch=fetch, persist=persist))
    persist.assert_awaited_once_with(None, [{
        "ts": START, "open": 11, "high": 11, "low": 11, "close": 11, "volume": 1,
        "figi": CNYRUB.storage_id, "interval": 1,
    }], window=(START, end))


def test_ingestion_filters_injected_source_at_closed_minute_boundary():
    end = START + timedelta(seconds=90)
    valid = {"ts": START, "open": 11, "high": 11, "low": 11, "close": 11, "volume": 1}
    bars = [dict(valid, ts=START - timedelta(minutes=1)), valid,
            dict(valid, ts=START + timedelta(minutes=1))]
    persist = AsyncMock()
    asyncio.run(sync_macro_series(None, CNYRUB, START, end,
                                  fetch=lambda *_: bars, persist=persist))
    persist.assert_awaited_once_with(
        None, [dict(valid, figi=CNYRUB.storage_id, interval=1)], window=(START, end)
    )


def test_hub_repairs_macro_gap_via_moex(monkeypatch):
    from types import SimpleNamespace

    from app.services import candle_hub_orchestrator as hub

    end = START + timedelta(minutes=1)
    figi = CNYRUB.storage_id
    bar = {"ts": START, "open": 11, "high": 11, "low": 11, "close": 11, "volume": 1}

    def read(instrument, start, stop):
        assert (instrument, start, stop) == (CNYRUB, START, end)
        return [bar]

    persist = AsyncMock(return_value=1)
    published = []
    cache = SimpleNamespace(
        gap_report=lambda *args, **kwargs: [(START, START)],
        seed_1m=lambda series, candles, **kwargs: published.extend(candles),
    )
    monkeypatch.setattr(hub, "read_closed_macro_candles", read)
    monkeypatch.setattr(hub, "upsert_candles", persist)
    result = asyncio.run(hub.CandleHubOrchestrator(cache).fill_gaps_detailed(None, figi))
    persist.assert_awaited_once_with(None, [dict(bar, figi=figi, interval=1)])
    assert result.committed == 1
    assert result.remaining_gaps == []
    assert [c.ts for c in published] == [START]


def test_tail_restarts_at_last_closed_stored_minute():
    from types import SimpleNamespace

    from app.services.candle_ingestion_contract import UpsertResult
    from app.services.macro_candle_ingestion import sync_macro_tail

    latest = START + timedelta(minutes=2)
    end = START + timedelta(minutes=5)
    db = SimpleNamespace(scalar=AsyncMock(return_value=latest))
    persist = AsyncMock(return_value=UpsertResult())

    def fetch(instrument, start, stop):
        assert (instrument, start, stop) == (CNYRUB, latest, end)
        return []

    asyncio.run(sync_macro_tail(db, CNYRUB, history_start=START, as_of=end,
                                fetch=fetch, persist=persist))
    persist.assert_awaited_once_with(db, [], window=(latest, end))


def test_macro_cycle_isolates_series_failure_and_skips_duplicates(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import macro_candle_ingestion as ingestion
    from app.services.candle_ingestion_contract import UpsertResult
    from app.services.macro_candle_source import IMOEX

    opened = []

    @asynccontextmanager
    async def session():
        db = object()
        opened.append(db)
        yield db

    async def sync(db, instrument, *, history_start, as_of):
        assert db is opened[-1]
        if instrument == CNYRUB:
            raise TimeoutError("ISS timeout")
        return UpsertResult(accepted=3, inserted=3)

    monkeypatch.setattr(ingestion, "_latest_macro_minute", AsyncMock(return_value=START))
    monkeypatch.setattr(ingestion, "sync_macro_tail", sync)
    report = asyncio.run(ingestion.sync_macro_cycle(
        session, (CNYRUB, CNYRUB, IMOEX), history_start=START, as_of=START + timedelta(minutes=3)
    ))
    assert len(opened) == 2
    assert report[CNYRUB.storage_id] == {"status": "error", "error_type": "TimeoutError"}
    assert report[IMOEX.storage_id]["status"] == "received"
    assert report[IMOEX.storage_id]["inserted"] == 3
    assert report[IMOEX.storage_id]["last_stored_close_at"] == (START + timedelta(minutes=1)).isoformat()


def test_source_stops_after_first_minute_beyond_window():
    calls = []

    def read_json(url):
        calls.append(url)
        assert len(calls) == 1
        return page("2026-10-09 10:00:00", "2026-10-09 10:01:00")

    bars = read_closed_macro_candles(
        CNYRUB, START, START + timedelta(minutes=1), read_json=read_json
    )
    assert [bar["ts"] for bar in bars] == [START]


def test_futures_catalog_keeps_two_nearest_unexpired_contracts():
    from app.services.macro_candle_source import listed_futures_for_collection

    table = {"securities": {
        "columns": ["SECID", "ASSETCODE", "LASTTRADEDATE"],
        "data": [
            ["BRG7", "BR", "2027-02-01"],
            ["BRU6", "BR", "2026-10-08"],
            ["BRX6", "BR", "2026-10-09"],
            ["BRZ6", "BR", "2026-12-01"],
            ["NGX6", "NG", "2026-11-25"],
        ],
    }}
    result = listed_futures_for_collection(("BR", "Si"), as_of=START, read_json=lambda _: table)
    assert [item.secid for item in result["BR"]] == ["BRX6", "BRZ6"]
    assert result["Si"] == ()


def test_macro_tail_batches_long_windows_without_day_boundary_holes():
    from types import SimpleNamespace

    from app.services.candle_ingestion_contract import UpsertResult
    from app.services.macro_candle_ingestion import sync_macro_tail

    end = START + timedelta(days=15)
    requests = []

    def fetch(instrument, start, stop):
        requests.append((start, stop))
        return []

    persist = AsyncMock(return_value=UpsertResult(accepted=1, inserted=1, written=1))
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    report = asyncio.run(sync_macro_tail(
        db, CNYRUB, history_start=START, as_of=end, fetch=fetch, persist=persist
    ))
    bounds = [START, START + timedelta(days=7), START + timedelta(days=14), end]
    assert requests == list(zip(bounds[:-1], bounds[1:]))
    assert report.inserted == report.written == 3
