from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.minute_activity import ClosedMinuteActivityGate
from app.engine.models import Candle
from app.engine.account_replay import run_shared_account
from tests.test_account_replay_golden import spec


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def candles(indices):
    return [Candle(T + i * M, 100, 100, 100, 100) for i in indices]


def test_threshold_and_window_are_closed_minute_based():
    assert ClosedMinuteActivityGate(candles(range(24)))(T + 30 * M)
    assert not ClosedMinuteActivityGate(candles(range(23)))(T + 30 * M)
    assert not ClosedMinuteActivityGate(candles(range(24)))(T + 31 * M)
    assert not ClosedMinuteActivityGate(candles(range(30)))(T + 23 * M)
    assert ClosedMinuteActivityGate(candles(range(30)))(T + 24 * M)


def test_future_candles_and_prices_cannot_change_earlier_admission():
    before = candles(range(23))
    future = [replace(bar, open=999, high=999, low=999, close=999) for bar in candles(range(30, 100))]
    assert not ClosedMinuteActivityGate(before + future)(T + 30 * M)
    assert ClosedMinuteActivityGate(before + candles([29]))(T + 30 * M)
    assert not ClosedMinuteActivityGate(before + candles([30]))(T + 30 * M)


def test_gate_runs_at_actual_open_and_rejection_keeps_future_signals_active():
    item = spec(candles=candles(range(6)), signal_indices=(0, 1, 2))
    item.entry_gate = ClosedMinuteActivityGate(item.candles, window_minutes=3, minimum_observations=3)
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    assert [r['at'] for r in result.rejections] == [T + M, T + 2 * M]
    assert result.ledgers['A'].trades[0].entry_time == T + 3 * M


def test_entry_gate_cannot_block_confirmed_position_exit():
    item = spec(candles=candles(range(4)))
    item.entry_gate = lambda at: at == T + M
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    assert len(result.ledgers['A'].trades) == 1
    assert result.final_equity == 10000


@pytest.mark.parametrize('values', [candles([1, 0]), candles([0, 0]),
    [Candle(T.replace(tzinfo=None), 100, 100, 100, 100)]])
def test_ambiguous_time_input_rejected(values):
    with pytest.raises(ValueError):
        ClosedMinuteActivityGate(values)
