"""Moscow month labels for the C7 coverage receipt (ARCH420).

The values in the manifest were measured in UTC; the labels people read must be
Moscow calendar months. These cases pin every boundary that the wrong version
got wrong: the 21:00Z month edge, a non-leap February, a leap February, and the
year rollover.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "reports" / "connected_week_20261109_13_c1" / "worker_c7_r1"
sys.path.insert(0, str(WORKER))

import coverage_manifest_r4 as r4  # noqa: E402

UTC = timezone.utc


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def test_january_boundary_is_moscow():
    # 2025-12-31T21:00Z is exactly 2026-01-01T00:00 MSK
    assert r4.msk_month_of("2025-12-31T21:00:00+00:00") == "2026-01"
    assert r4.msk_month_of(utc(2025, 12, 31, 21, 0)) == "2026-01"


def test_one_minute_before_the_boundary_is_december():
    assert r4.msk_month_of(utc(2025, 12, 31, 20, 59)) == "2025-12"


def test_month_start_keeps_both_representations():
    start_utc = r4.msk_month_start_utc(utc(2025, 12, 31, 21, 0))
    start_local = r4.msk_month_start_local(utc(2025, 12, 31, 21, 0))
    assert start_utc == utc(2025, 12, 31, 21, 0)
    assert start_local.isoformat() == "2026-01-01T00:00:00+03:00"
    assert start_local.utcoffset().total_seconds() == 3 * 3600


def test_non_leap_february_edge():
    assert r4.msk_month_of(utc(2026, 2, 28, 21, 0)) == "2026-03"
    assert r4.msk_month_of(utc(2026, 2, 28, 20, 59)) == "2026-02"


def test_leap_february_edge():
    assert r4.msk_month_of(utc(2028, 2, 29, 21, 0)) == "2028-03"
    assert r4.msk_month_of(utc(2028, 2, 29, 20, 59)) == "2028-02"


def test_year_rollover():
    assert r4.msk_month_of(utc(2026, 12, 31, 21, 0)) == "2027-01"
    assert r4.msk_month_start_local(utc(2026, 12, 31, 21, 0)).isoformat() == "2027-01-01T00:00:00+03:00"


def test_window_end_is_september_in_moscow():
    # the frozen window ends at 2026-09-01T00:00 MSK
    assert r4.msk_month_of(utc(2026, 8, 31, 21, 0)) == "2026-09"


def test_mid_month_instant_stays_in_its_month():
    assert r4.msk_month_of(utc(2026, 3, 15, 12, 0)) == "2026-03"
    assert r4.msk_month_start_utc(utc(2026, 3, 15, 12, 0)) == utc(2026, 2, 28, 21, 0)


def test_relabel_keeps_measured_values_and_the_parent_label():
    row = {
        "month_msk": "2025-12",
        "month_start_msk": "2025-12-31T21:00:00+00:00",
        "month_start_utc": "2025-12-31T21:00:00+00:00",
        "minutes_1m": 691088,
        "days_with_data": 30,
        "figis_present": 29,
        "figis_eligible_at_month_start": 10,
    }
    fixed = r4.relabel_month(row)
    assert fixed["month_msk"] == "2026-01"
    assert fixed["month_start_utc"] == row["month_start_utc"]
    assert fixed["month_start_msk"] == "2026-01-01T00:00:00+03:00"
    assert fixed["minutes_1m"] == row["minutes_1m"]
    assert fixed["figis_eligible_at_month_start"] == row["figis_eligible_at_month_start"]
    assert fixed["parent_month_msk"] == "2025-12"
    assert fixed["correction"] == "label_and_offset_corrected"


def test_already_correct_row_is_marked_unchanged():
    row = {
        "month_msk": "2026-03",
        "month_start_msk": "2026-02-28T21:00:00+00:00",
        "month_start_utc": "2026-02-28T21:00:00+00:00",
        "minutes_1m": 826596,
        "days_with_data": 28,
        "figis_present": 29,
        "figis_eligible_at_month_start": 28,
    }
    assert r4.relabel_month(row)["correction"] == "unchanged"


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError):
        r4.msk_month_of(datetime(2025, 12, 31, 21, 0))
