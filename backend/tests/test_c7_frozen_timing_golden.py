"""Golden timing checks for the frozen C7 composition (15m -> 5m -> 1m).

These are the small checks root asked for before any evaluation: the 15m bar is
invisible before its close, the confirming 5m bucket is exactly the one starting
at T, a missing or late confirmation cancels and is never revived, the 5m SELL
quorum vetoes, execution is the 1m open at T+5, and the paired control stays the
unchanged 15m entry at T.

Synthetic bars only: no database, no Hub, no runner, no output written.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "reports" / "connected_week_20261109_13_c1" / "worker_c7_r1"
sys.path.insert(0, str(WORKER_DIR))

import c7_timing as t7  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)  # 15m close


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1.0


def bar(ts: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts, o, h, l, c)


def signal_bars_before(count: int = 130, last_high: float = 101.0, last_low: float = 99.0):
    """130 closed 15m bars ending with the bucket [T-15, T)."""
    out = []
    start = T - timedelta(minutes=15 * count)
    for i in range(count):
        ts = start + timedelta(minutes=15 * i)
        if i == count - 1:
            out.append(bar(ts, 100.0, last_high, last_low, 100.5))
        else:
            out.append(bar(ts, 100.0, 100.6, 99.4, 100.0))
    return out


def signal_map_all_buy(setups: int = 2, label: datetime | None = None) -> dict:
    label = label if label is not None else T - timedelta(minutes=15)
    return {f"setup{i}": {label: "BUY"} for i in range(setups)}


def four_bars_around_T() -> list:
    """1m bars covering [T,T+1), [T+4,T+5), [T+5,T+6), [T+6,T+7)."""
    return [
        bar(T + timedelta(minutes=m), 200.0 + m, 201.0 + m, 199.0 + m, 200.5 + m)
        for m in (0, 4, 5, 6)
    ]


# --- 1. visibility of the signal bucket -------------------------------------


def test_15m_bar_is_invisible_before_its_close():
    bars = signal_bars_before(3)
    signal_label = T - timedelta(minutes=15)
    # one microsecond before the close the bucket is still open
    early = t7.last_closed_bar(bars, t7.SIGNAL_TF, T - timedelta(microseconds=1))
    assert early.ts == signal_label - timedelta(minutes=15)
    # at the close it becomes readable
    closed = t7.last_closed_bar(bars, t7.SIGNAL_TF, T)
    assert closed.ts == signal_label


def test_as_of_inside_the_bucket_cannot_see_it():
    bars = signal_bars_before(3)
    mid = T - timedelta(minutes=1)
    assert t7.last_closed_bar(bars, t7.SIGNAL_TF, mid).ts == T - timedelta(minutes=30)


# --- 2. exact confirmation bucket ------------------------------------------


def test_confirmation_is_the_bucket_starting_exactly_at_T():
    bars5 = [bar(T - timedelta(minutes=5), 100, 100.5, 99.5, 100), bar(T, 100.5, 102, 99.5, 101.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    assert t7.confirm_at(signal, bars5, {}, four_bars_around_T()).state == t7.CONFIRM


def test_later_bucket_does_not_confirm():
    # only [T+5,T+10) exists: nothing starts exactly at T
    bars5 = [bar(T + timedelta(minutes=5), 100.5, 103, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    d = t7.confirm_at(signal, bars5, {}, four_bars_around_T())
    assert d.state == t7.CANCEL_MISSING_CONFIRMATION
    assert d.reason == "no_exact_5m_bucket_at_T"
    assert d.execution_at is None


def test_earlier_bucket_does_not_confirm():
    bars5 = [bar(T - timedelta(minutes=5), 100.0, 105.0, 98.0, 104.0)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    assert t7.confirm_at(signal, bars5, {}, four_bars_around_T()).state == t7.CANCEL_MISSING_CONFIRMATION


# --- 3. missing confirmation cancels and is never revived -------------------


def test_missing_confirmation_cancels():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    d = t7.confirm_at(signal, [], {}, four_bars_around_T())
    assert d.state == t7.CANCEL_MISSING_CONFIRMATION
    assert d.confirmation_ts is None


def test_confirmation_is_not_readable_at_T_plus_4():
    # the bucket [T,T+5) exists in the feed, but at T+4 the window is still open
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    d = t7.confirm_at(signal, bars5, {}, four_bars_around_T(), as_of=T + timedelta(minutes=4))
    assert d.state == t7.WAIT_NOT_VISIBLE
    assert d.execution_at is None


def test_confirmation_is_decided_exactly_at_T_plus_5():
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    d = t7.confirm_at(signal, bars5, {}, four_bars_around_T(), as_of=T + timedelta(minutes=5))
    assert d.state == t7.CONFIRM
    assert d.execution_at == T + timedelta(minutes=5)


def test_late_same_label_bucket_cannot_revive_an_expired_window():
    # nothing was applied at T+5; at T+6 the very same bucket shows up
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    late = t7.confirm_at(signal, bars5, {}, four_bars_around_T(), as_of=T + timedelta(minutes=6))
    assert late.state == t7.CANCEL_EXPIRED
    assert late.execution_at is None
    assert late.execution_open is None


def test_pending_state_freezes_the_T_plus_5_result():
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    pending = t7.PendingConfirmation(signal)
    assert pending.observe(T + timedelta(minutes=4), bars5, {}, four_bars_around_T()) is None
    assert not pending.resolved
    confirmed = pending.observe(T + timedelta(minutes=5), bars5, {}, four_bars_around_T())
    assert confirmed.state == t7.CONFIRM
    assert pending.resolved and pending.applied_at == T + timedelta(minutes=5)
    # a later step returns the frozen decision even with a hostile feed
    broken = [bar(T, 100.5, 100.6, 10.0, 100.6)]
    assert pending.observe(T + timedelta(minutes=6), broken, {}, four_bars_around_T()) == confirmed


def test_pending_state_expires_when_T_plus_5_was_skipped():
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    pending = t7.PendingConfirmation(signal)
    expired = pending.observe(T + timedelta(minutes=6), bars5, {}, four_bars_around_T())
    assert expired.state == t7.CANCEL_EXPIRED
    assert pending.observe(T + timedelta(minutes=7), bars5, {}, four_bars_around_T()) == expired


# --- 4. SELL veto -----------------------------------------------------------


def test_sell_quorum_vetoes_even_a_confirmed_bar():
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    veto = {"a": {T: "SELL"}, "b": {T: "SELL"}}
    d = t7.confirm_at(signal, bars5, veto, four_bars_around_T())
    assert d.state == t7.CANCEL_VETO_SELL
    assert (d.sell_votes, d.buy_votes) == (2, 0)
    assert d.execution_at is None


def test_single_sell_vote_and_tie_do_not_veto():
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    assert t7.confirm_at(signal, bars5, {"a": {T: "SELL"}}, four_bars_around_T()).state == t7.CONFIRM
    tie = {"a": {T: "SELL"}, "b": {T: "SELL"}, "c": {T: "BUY"}, "d": {T: "BUY"}}
    assert t7.confirm_at(signal, bars5, tie, four_bars_around_T()).state == t7.CONFIRM


def test_conflicting_votes_in_one_setup_are_rejected():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    with pytest.raises(ValueError):
        t7.confirm_at(signal, [bar(T, 100.5, 103, 99.5, 102.5)], {"a": {T: {"BUY", "SELL"}}}, four_bars_around_T())


# --- 5. price rules --------------------------------------------------------


def test_close_must_be_strictly_above_signal_high():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    equal = [bar(T, 100.5, 101.0, 99.5, 101.0)]
    d = t7.confirm_at(signal, equal, {}, four_bars_around_T())
    assert d.state == t7.CANCEL_NO_CLOSE_ABOVE_HIGH
    above = [bar(T, 100.5, 101.4, 99.6, 101.2)]
    assert t7.confirm_at(signal, above, {}, four_bars_around_T()).state == t7.CONFIRM


def test_low_may_touch_but_not_break_signal_low():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    touch = [bar(T, 100.5, 102.0, 99.0, 101.5)]
    assert t7.confirm_at(signal, touch, {}, four_bars_around_T()).state == t7.CONFIRM
    broken = [bar(T, 100.5, 102.0, 98.99, 101.5)]
    d = t7.confirm_at(signal, broken, {}, four_bars_around_T())
    assert d.state == t7.CANCEL_SIGNAL_LOW_BROKEN


# --- 6. execution and control ----------------------------------------------


def test_execution_is_the_1m_open_at_T_plus_5():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    bars1 = four_bars_around_T()
    d = t7.confirm_at(signal, [bar(T, 100.5, 103.0, 99.5, 102.5)], {}, bars1)
    assert d.state == t7.CONFIRM
    assert d.execution_at == T + timedelta(minutes=5)
    assert d.execution_open == 205.0  # open of the bucket starting at T+5


def test_infinite_confirmation_close_is_rejected():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    broken = [bar(T, 100.5, 103.0, 99.5, float("inf"))]
    d = t7.confirm_at(signal, broken, {}, four_bars_around_T())
    assert d.state == t7.CANCEL_MISSING_CONFIRMATION
    assert d.reason == "invalid_5m_bucket_at_T"


def test_negative_and_infinite_execution_open_are_rejected():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    good5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    for open_value in (-1.0, float("inf"), 0.0):
        bars1 = [b for b in four_bars_around_T() if b.ts != T + timedelta(minutes=5)]
        bars1.append(bar(T + timedelta(minutes=5), open_value, 201.0, 199.0, 200.5))
        d = t7.confirm_at(signal, good5, {}, bars1)
        assert d.state == t7.CANCEL_MISSING_CONFIRMATION, open_value
        assert d.reason == "no_exact_1m_open_at_T_plus_5"
        assert d.execution_open is None


def test_inconsistent_confirmation_ohlc_is_rejected():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    for broken_bar in (
        bar(T, 100.5, 100.0, 99.5, 100.2),   # high below close
        bar(T, 100.5, 103.0, 103.5, 102.0),  # low above high
        bar(T, 0.0, 103.0, 99.5, 102.5),     # non-positive open
    ):
        d = t7.confirm_at(signal, [broken_bar], {}, four_bars_around_T())
        assert d.state == t7.CANCEL_MISSING_CONFIRMATION


def test_invalid_signal_bar_is_not_traded():
    bars = signal_bars_before()
    bars[-1] = bar(T - timedelta(minutes=15), 100.0, 100.0, 99.0, float("nan"))
    signal, reason, _, _ = t7.signal_at("X", bars, signal_map_all_buy(), T)
    assert signal is None and reason == "invalid_signal_bar"


def test_missing_1m_open_at_T_plus_5_cancels_without_later_open():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    bars1 = [b for b in four_bars_around_T() if b.ts != T + timedelta(minutes=5)]
    d = t7.confirm_at(signal, [bar(T, 100.5, 103.0, 99.5, 102.5)], {}, bars1)
    assert d.state == t7.CANCEL_MISSING_CONFIRMATION
    assert d.reason == "no_exact_1m_open_at_T_plus_5"
    assert d.execution_open is None


def test_control_enters_at_T_unchanged():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    d = t7.control_decision(signal, four_bars_around_T())
    assert d.arm == t7.ARM_CONTROL
    assert d.state == t7.CONFIRM
    assert d.execution_at == T
    assert d.execution_open == 200.0  # open of the bucket starting at T


def test_control_has_no_confirmation_step():
    # confirmation bucket broken, control still enters: arms differ only by timing
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    broken5 = [bar(T, 100.5, 100.6, 90.0, 100.6)]
    assert t7.confirm_at(signal, broken5, {}, four_bars_around_T()).state == t7.CANCEL_SIGNAL_LOW_BROKEN
    assert t7.control_decision(signal, four_bars_around_T()).state == t7.CONFIRM


# --- 7. signal admission ---------------------------------------------------


def test_no_signal_without_buy_quorum():
    signal, reason, buy, sell = t7.signal_at("X", signal_bars_before(), {"a": {T - timedelta(minutes=15): "BUY"}}, T)
    assert signal is None and reason == "no_buy_quorum" and (buy, sell) == (1, 0)


def test_buy_must_strictly_exceed_sell():
    votes = {
        "a": {T - timedelta(minutes=15): "BUY"},
        "b": {T - timedelta(minutes=15): "BUY"},
        "c": {T - timedelta(minutes=15): "SELL"},
        "d": {T - timedelta(minutes=15): "SELL"},
    }
    assert t7.signal_at("X", signal_bars_before(), votes, T)[0] is None


def test_insufficient_history_blocks_signal():
    signal, reason, _, _ = t7.signal_at("X", signal_bars_before(3), signal_map_all_buy(), T)
    assert signal is None and reason == "insufficient_signal_history"


def test_stale_bar_cannot_stand_in_for_the_boundary():
    bars = [b for b in signal_bars_before() if b.ts != T - timedelta(minutes=15)]
    signal, reason, _, _ = t7.signal_at("X", bars, signal_map_all_buy(), T)
    assert signal is None and reason == "no_exact_signal_bar"


# --- 8. bucket labels ------------------------------------------------------


# --- 9. causal eligibility across the pool ---------------------------------


def _labels_closing_at(end_close: datetime, minutes: int, count: int) -> list:
    last_label = end_close - timedelta(minutes=minutes)
    return [last_label - timedelta(minutes=minutes * i) for i in reversed(range(count))]


def _eligibility_fixture():
    """Depth met at 10:05, the next present 15m close is 10:30."""
    end = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
    labels5 = _labels_closing_at(end + timedelta(minutes=5), 5, 120)   # ready 10:05
    labels15 = _labels_closing_at(end, 15, 120)                        # ready 10:00
    labels60 = _labels_closing_at(end, 60, 20)                         # ready 10:00
    labels15.append(datetime(2026, 3, 10, 10, 15, tzinfo=UTC))
    labels5.append(datetime(2026, 3, 10, 10, 25, tzinfo=UTC))
    return labels5, labels15, labels60


def test_depth_ready_is_not_reported_as_a_tradable_close():
    labels5, labels15, labels60 = _eligibility_fixture()
    ready, first, reason = t7.first_eligible_decision_at(labels5, labels15, labels60)
    assert ready == datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
    # no 15m bar at 10:15→close 10:15, so the first real close is 10:30
    assert first == datetime(2026, 3, 10, 10, 30, tzinfo=UTC)
    assert reason == "present_close_with_fresh_evidence"


def test_delayed_next_bar_skips_a_close_without_fresh_5m():
    labels5, labels15, labels60 = _eligibility_fixture()
    # drop the 5m bucket that would have confirmed the 10:30 close
    labels5 = [x for x in labels5 if x != datetime(2026, 3, 10, 10, 25, tzinfo=UTC)]
    labels15.append(datetime(2026, 3, 10, 10, 30, tzinfo=UTC))   # closes 10:45
    labels15.append(datetime(2026, 3, 10, 11, 0, tzinfo=UTC))    # closes 11:15
    labels5.append(datetime(2026, 3, 10, 11, 10, tzinfo=UTC))    # fresh for 11:15
    labels60.append(datetime(2026, 3, 10, 10, 0, tzinfo=UTC))    # closes 11:00
    ready, first, reason = t7.first_eligible_decision_at(labels5, labels15, labels60)
    assert ready == datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
    # 10:30 and 10:45 have 15m bars but no 5m bucket closing at that instant
    assert first == datetime(2026, 3, 10, 11, 15, tzinfo=UTC)
    assert reason == "present_close_with_fresh_evidence"


def test_missing_hourly_bar_blocks_a_close():
    labels5, labels15, labels60 = _eligibility_fixture()
    labels5 = [
        x for x in labels5
        if x not in (datetime(2026, 3, 10, 10, 25, tzinfo=UTC),)
    ]
    labels15 = [
        x for x in labels15
        if x != datetime(2026, 3, 10, 10, 15, tzinfo=UTC)
    ]
    labels5.append(datetime(2026, 3, 10, 11, 10, tzinfo=UTC))
    labels15.append(datetime(2026, 3, 10, 11, 0, tzinfo=UTC))    # closes 11:15
    # no 60m bucket closing at 11:00, so the hourly trend would be stale
    ready, first, reason = t7.first_eligible_decision_at(labels5, labels15, labels60)
    assert first is None
    assert reason == "no_present_15m_close_with_fresh_5m_and_60m_evidence"


def test_insufficient_depth_reports_which_frame_is_short():
    labels5 = _labels_closing_at(datetime(2026, 3, 10, 10, 0, tzinfo=UTC), 5, 120)
    labels15 = _labels_closing_at(datetime(2026, 3, 10, 10, 0, tzinfo=UTC), 15, 30)
    labels60 = _labels_closing_at(datetime(2026, 3, 10, 10, 0, tzinfo=UTC), 60, 19)
    ready, first, reason = t7.first_eligible_decision_at(labels5, labels15, labels60)
    assert ready is None and first is None
    assert reason == "insufficient_depth:15m_30_of_120,60m_19_of_20"


def test_eligibility_can_be_required_inside_the_window():
    labels5, labels15, labels60 = _eligibility_fixture()
    window_start = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)
    ready, first, reason = t7.eligibility_after_window_start(
        labels5, labels15, labels60, window_start,
    )
    assert ready == datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
    assert first is None
    assert reason == "no_present_15m_close_inside_window"


def test_consumer_enters_once_from_a_repeated_apply():
    signal = t7.signal_at("X", signal_bars_before(), signal_map_all_buy(), T)[0]
    pending = t7.PendingConfirmation(signal)
    bars5 = [bar(T, 100.5, 103.0, 99.5, 102.5)]
    entries = []
    trace = []
    for minute in range(4, 9):
        decision = t7.apply_delayed(
            pending, T + timedelta(minutes=minute), bars5, {}, four_bars_around_T(),
        )
        trace.append(None if decision is None else decision.state)
        if decision is not None and decision.accepted and not entries:
            entries.append(decision)
    # WAIT at T+4, CONFIRM at T+5, then the stored decision repeats
    assert trace[0] is None
    assert trace[1] == t7.CONFIRM
    assert trace[2:] == [t7.CONFIRM, t7.CONFIRM, t7.CONFIRM]
    # the portfolio side must consume it once, not once per minute
    assert len(entries) == 1
    assert entries[0].execution_at == T + timedelta(minutes=5)


def test_floor_to_utc_buckets():
    when = datetime(2026, 3, 10, 10, 7, 33, tzinfo=UTC)
    assert t7.floor_to(when, 15) == datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
    assert t7.floor_to(when, 5) == datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
    assert t7.floor_to(when, 60) == datetime(2026, 3, 10, 10, 0, tzinfo=UTC)


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError):
        t7.last_closed_bar([], t7.SIGNAL_TF, datetime(2026, 3, 10, 10, 0))
    naive = datetime(2026, 3, 10, 10, 0)
    with pytest.raises(ValueError):
        t7.bar_starting_at([], naive)


def test_boundary_record_pairs_both_arms():
    rec = t7.evaluate_15m_boundary(
        "X", signal_bars_before(), [bar(T, 100.5, 103.0, 99.5, 102.5)], four_bars_around_T(),
        signal_map_all_buy(), {}, T,
    )
    assert rec["signal_reason"] == "buy_quorum"
    assert rec["control"].execution_at == T
    # the C7 arm carries no future outcome: only a pending window to apply at T+5
    assert rec["pending"].resolved is False
    assert rec["pending"].expires_at == T + timedelta(minutes=5)
    applied = t7.apply_delayed(
        rec["pending"], T + timedelta(minutes=5),
        [bar(T, 100.5, 103.0, 99.5, 102.5)], {}, four_bars_around_T(),
    )
    assert applied.state == t7.CONFIRM
    assert applied.execution_at == T + timedelta(minutes=5)


def test_boundary_has_no_pending_object_without_a_signal():
    rec = t7.evaluate_15m_boundary(
        "X", signal_bars_before(), [bar(T, 100.5, 103.0, 99.5, 102.5)], four_bars_around_T(),
        {"a": {T - timedelta(minutes=15): "BUY"}}, {}, T,
    )
    assert rec["pending"] is None and rec["control"] is None
    assert rec["signal_reason"] == "no_buy_quorum"
