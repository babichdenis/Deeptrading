from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import DecisionAction, PositionState, Side, Signal
from scripts.research.architect_context_clock import ExecutionClockContextPolicy
from scripts.research.architect_relative_gate import ContextPolicy


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def snapshot(minutes, relative=.02, moex=-.01):
    return {'as_of': (T + timedelta(minutes=minutes)).isoformat(),
            'features': {'SBER': {'valid': True, 'relative_return': relative, 'moex_return': moex}}}


def signal(minutes=0, side=Side.BUY):
    return Signal('fixture', side, T + timedelta(minutes=minutes))


def test_minute_execution_does_not_read_five_minute_future_context():
    snapshots = [snapshot(0, -.02, .01), snapshot(5)]
    policy = ExecutionClockContextPolicy('SBER', snapshots, execution_timeframe=timedelta(minutes=1))
    assert policy.decide(signal(), PositionState.FLAT, 0)[0] is DecisionAction.NO_SIGNAL
    assert ContextPolicy('SBER', snapshots).decide(signal(), PositionState.FLAT, 0)[0] is DecisionAction.ACCEPT_ENTRY
    assert policy.decide(signal(4), PositionState.FLAT, 0)[0] is DecisionAction.ACCEPT_ENTRY


@pytest.mark.parametrize('minutes', [1, 5, 15, 60])
def test_exact_close_visibility_both_directions(minutes):
    policy = ExecutionClockContextPolicy('SBER', [snapshot(minutes), snapshot(minutes * 2, -.02, .01)],
                                         execution_timeframe=timedelta(minutes=minutes))
    assert policy.decide(signal(-1), PositionState.FLAT, 0)[0] is DecisionAction.NO_SIGNAL
    assert policy.decide(signal(), PositionState.FLAT, 0)[0] is DecisionAction.ACCEPT_ENTRY
    assert policy.decide(signal(minutes, Side.SELL), PositionState.FLAT, 0)[0] is DecisionAction.ACCEPT_ENTRY


def test_five_minute_parity_and_held_exit_preserved():
    snapshots = [snapshot(0), snapshot(15, -.02, .01)]
    old = ContextPolicy('SBER', snapshots)
    new = ExecutionClockContextPolicy('SBER', snapshots, execution_timeframe=timedelta(minutes=5))
    for minute in range(-10, 30):
        for side in Side:
            for state in PositionState:
                assert new.decide(signal(minute, side), state, 0) == old.decide(signal(minute, side), state, 0)
    unknown = ExecutionClockContextPolicy('SBER', [], execution_timeframe=timedelta(minutes=1))
    assert unknown.decide(signal(side=Side.SELL), PositionState.LONG, 0)[0] is DecisionAction.ACCEPT_EXIT


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, True])
def test_unknown_context_cannot_approve_entry(value):
    policy = ExecutionClockContextPolicy('SBER', [snapshot(0, value)], execution_timeframe=timedelta(minutes=1))
    assert policy.decide(signal(), PositionState.FLAT, 0)[0] is DecisionAction.NO_SIGNAL


def test_context_timeline_must_be_unambiguous():
    for rows in ([snapshot(5), snapshot(0)], [snapshot(0), snapshot(0)],
                 [{'as_of': T.replace(tzinfo=None).isoformat()}]):
        with pytest.raises(ValueError):
            ExecutionClockContextPolicy('SBER', rows, execution_timeframe=timedelta(minutes=1))
