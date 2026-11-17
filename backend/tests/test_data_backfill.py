import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.services import data_backfill


def test_candidates_include_weekend_at_moscow_boundary():
    start = datetime(2026, 10, 30, 21, tzinfo=timezone.utc)
    days = data_backfill._candidate_days(start, start + timedelta(days=2))

    assert [day.date().isoformat() for day in days] == [
        "2026-10-31", "2026-11-01", "2026-11-02"
    ]


def test_backfill_reports_source_failure_separately_from_empty(monkeypatch):
    start = datetime(2026, 10, 31, tzinfo=data_backfill.MSK)
    days = [start + timedelta(days=offset) for offset in range(4)]

    def fetch(ticker, day):
        assert ticker == "SBER"
        if day in days[:2]:
            raise TimeoutError
        return []

    monkeypatch.setattr(data_backfill, "_candidate_days", lambda *_: days)
    monkeypatch.setattr(data_backfill, "_candle_counts_by_day", AsyncMock(return_value={days[3].date(): 58}))
    monkeypatch.setattr(data_backfill, "_fetch_day_moex", fetch)
    monkeypatch.setattr(data_backfill, "_write_moex_day", AsyncMock(return_value=0))
    monkeypatch.setattr(data_backfill, "_backfill_day_tinvest", AsyncMock(side_effect=[4, 0, 0]))

    report = asyncio.run(data_backfill.run_backfill([("FIGI", "SBER")], log=lambda _: None))

    assert report["coverage_basis"] == "calendar_day_presence"
    assert report["initial_minutes_by_day"]["FIGI"] == {
        "2026-10-31": 0, "2026-11-01": 0, "2026-11-02": 0, "2026-11-03": 58
    }
    assert report["by_figi"]["FIGI"]["sources"] == {
        "moex": 0, "tinvest": 1, "empty": 1, "error": 1
    }
    assert report["total_added"] == 4
    assert [entry["sources"] for entry in report["source_errors"]] == [
        {"moex": "TimeoutError"}, {"moex": "TimeoutError"}
    ]
