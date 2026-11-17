"""Paired composition runner: one entry per signal per arm (ARCH418/ARCH420).

Synthetic bars only. These cases are the composition gate: control and C7 on one
grid, the stored CONFIRM consumed once, and every cancel state visible in the
trace instead of silently entering.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "reports" / "connected_week_20261109_13_c1" / "worker_c7_r1"
sys.path.insert(0, str(WORKER))

import c7_timing as t7  # noqa: E402
import paired_composition as pc  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
FIGI = "BBG004730N88"


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


def signal_bars(count: int = 130, high: float = 101.0, low: float = 99.0):
    """15m bars whose last bucket is labelled T-15, so the signal closes at T."""
    last = T - timedelta(minutes=15)
    return [
        Bar(last - timedelta(minutes=15 * i), 100.0, high, low, 100.0)
        for i in reversed(range(count))
    ]


def buy_map(label: datetime | None = None, setups: int = 2):
    """Quorum 2: two setups voting BUY on the signal bucket."""
    label = T - timedelta(minutes=15) if label is None else label
    return {f"setup{i}": {label: "BUY"} for i in range(setups)}


def one_minutes(*opens):
    """1m bars starting at T, T+1, ... with the given opens."""
    return [
        Bar(T + timedelta(minutes=index), price, price, price, price)
        for index, price in enumerate(opens)
    ]


def confirm5(close: float = 102.5):
    """The 5m bucket [T, T+5) that becomes readable at T+5."""
    return [Bar(T, 100.5, max(103.0, close), 99.5, close)]


def test_control_and_c7_both_enter_on_a_confirmed_signal():
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203, 204, 210),
        confirm5(102.5), signal_bars(), buy_map(), {}, minutes_after=6,
    )
    filled = [e for e in events if e.state == "FILLED"]
    assert [e.arm for e in filled] == [t7.ARM_CONTROL, t7.ARM_C7]
    control, c7 = filled
    assert control.execution_at == T and control.price == 200.0
    assert c7.execution_at == T + timedelta(minutes=5) and c7.price == 210.0
    assert pc.count_entries(events) == {t7.ARM_CONTROL: 1, t7.ARM_C7: 1}


def pc_sell_veto():
    return {"setup0": {T: "SELL"}, "setup1": {T: "SELL"}}


def test_sell_veto_skips_c7_but_not_the_control():
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203, 204, 210),
        confirm5(102.5), signal_bars(), buy_map(),
        pc_sell_veto(),
    )
    states = {(e.arm, e.state) for e in events}
    assert (t7.ARM_CONTROL, "FILLED") in states
    assert (t7.ARM_C7, "SKIPPED") in states
    skipped = [e for e in events if e.arm == t7.ARM_C7 and e.state == "SKIPPED"][0]
    assert skipped.reason == t7.CANCEL_VETO_SELL
    assert pc.count_entries(events) == {t7.ARM_CONTROL: 1, t7.ARM_C7: 0}


def test_confirmation_that_closes_at_the_high_is_not_an_entry():
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203, 204, 210),
        confirm5(101.0), signal_bars(), buy_map(), {},
    )
    skipped = [e for e in events if e.arm == t7.ARM_C7][0]
    assert skipped.state == "SKIPPED"
    assert skipped.reason == t7.CANCEL_NO_CLOSE_ABOVE_HIGH
    assert pc.count_entries(events) == {t7.ARM_CONTROL: 1, t7.ARM_C7: 0}


def test_missing_execution_minute_cancels_instead_of_pricing_elsewhere():
    # only four 1m bars: there is no bar starting at T+5
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203),
        confirm5(102.5), signal_bars(), buy_map(), {},
    )
    skipped = [e for e in events if e.arm == t7.ARM_C7][0]
    assert skipped.state == "SKIPPED"
    assert skipped.price is None
    assert pc.count_entries(events) == {t7.ARM_CONTROL: 1, t7.ARM_C7: 0}


def test_signals_without_quorum_produce_no_events():
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203, 204, 210),
        confirm5(102.5), signal_bars(), buy_map(setups=1), {},
    )
    assert events == []


def test_repeated_minutes_never_duplicate_an_entry():
    bars1 = one_minutes(200, 201, 202, 203, 204, 210)
    bars5 = confirm5(102.5)
    events = pc.run_paired(
        FIGI, [T], bars1, bars5, signal_bars(), buy_map(), {}, minutes_after=9,
    )
    c7 = [e for e in events if e.arm == t7.ARM_C7]
    assert len(c7) == 1
    assert c7[0].state == "FILLED"
    assert c7[0].execution_at == T + timedelta(minutes=5)


def test_two_boundaries_keep_arm_entries_separate():
    other = T + timedelta(minutes=15)
    bars15 = signal_bars() + [
        Bar(T, 100.0, 101.0, 99.0, 100.0),
        Bar(T + timedelta(minutes=15), 100.0, 101.0, 99.0, 100.0),
    ]
    votes = {
        "setup0": {T - timedelta(minutes=15): "BUY", other - timedelta(minutes=15): "BUY"},
        "setup1": {T - timedelta(minutes=15): "BUY", other - timedelta(minutes=15): "BUY"},
    }
    bars1 = one_minutes(*([200.0] * 30))
    bars5 = confirm5(102.5) + [
        Bar(other, 100.5, 103.0, 99.5, 102.5),
    ]
    events = pc.run_paired(
        FIGI, [T, other], bars1, bars5, bars15, votes, {},
    )
    assert pc.count_entries(events) == {t7.ARM_CONTROL: 2, t7.ARM_C7: 2}
    c7_times = [e.execution_at for e in events if e.arm == t7.ARM_C7 and e.state == "FILLED"]
    assert c7_times == [T + timedelta(minutes=5), other + timedelta(minutes=5)]


def test_trace_is_json_ready():
    events = pc.run_paired(
        FIGI, [T], one_minutes(200, 201, 202, 203, 204, 210),
        confirm5(102.5), signal_bars(), buy_map(), {},
    )
    row = events[0].as_dict()
    assert row["arm"] == t7.ARM_CONTROL
    assert row["signal_ts"].endswith("+00:00")
