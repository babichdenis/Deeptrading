from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine import EngineConfig, EngineRunner, FixedSlTpPolicy, Side, Signal
from app.engine.models import Candle
from app.engine.timeframe_signal import TimeframeSignalAdapter


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


class EveryBar:
    strategy_id = 'every_bar'

    def reset(self):
        self.seen = []

    def warmup_bars(self):
        return 2

    def on_bar(self, bars):
        self.seen.append(bars[-1])
        return Signal(self.strategy_id, Side.BUY, bars[-1].ts,
                      features={'observed_sum': sum(bar.close for bar in bars)})


def bars(n, minutes):
    return [Candle(T + timedelta(minutes=i * minutes), 100, 100, 100, 100) for i in range(n)]


@pytest.mark.parametrize('signal_minutes, execution_minutes', [(5, 1), (15, 5), (60, 5)])
def test_signal_only_at_completed_higher_bar_after_own_warmup(signal_minutes, execution_minutes):
    strategy = EveryBar()
    source = bars(3, signal_minutes)
    adapter = TimeframeSignalAdapter(strategy, source, signal_timeframe=timedelta(minutes=signal_minutes),
                                     execution_timeframe=timedelta(minutes=execution_minutes))
    execution = bars(3 * signal_minutes // execution_minutes, execution_minutes)
    emitted = []
    for i in range(len(execution)):
        signal = adapter.on_bar(execution[:i + 1])
        assert all(b.ts + timedelta(minutes=signal_minutes) <= execution[i].ts + timedelta(minutes=execution_minutes)
                   for b in strategy.seen)
        if signal:
            emitted.append(signal)
    assert [s.time for s in emitted] == [T + timedelta(minutes=n * signal_minutes - execution_minutes) for n in (2, 3)]
    assert emitted[0].features['source_signal_bar_start'] == source[1].ts.timestamp()
    assert len(strategy.seen) == 3


def test_missing_execution_interval_does_not_replay_stale_entry():
    strategy = EveryBar()
    adapter = TimeframeSignalAdapter(strategy, bars(4, 15), signal_timeframe=timedelta(minutes=15),
                                     execution_timeframe=timedelta(minutes=5))
    execution = [Candle(T + timedelta(minutes=35), 100, 100, 100, 100)]
    assert adapter.on_bar(execution) is None
    assert len(strategy.seen) == 2


def test_future_signal_bar_mutation_cannot_change_earlier_decision():
    source = bars(4, 15)
    mutated = source[:3] + [replace(source[3], open=999, high=999, low=999, close=999)]
    outcomes = []
    for values in (source, mutated):
        adapter = TimeframeSignalAdapter(EveryBar(), values, signal_timeframe=timedelta(minutes=15),
                                         execution_timeframe=timedelta(minutes=5))
        outcomes.append(adapter.on_bar(bars(6, 5)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] is not None
    assert outcomes[0].features['observed_sum'] == 200


def test_engine_entry_at_next_lower_frame_open_and_repeat_run():
    adapter = TimeframeSignalAdapter(EveryBar(), bars(3, 15), signal_timeframe=timedelta(minutes=15),
                                     execution_timeframe=timedelta(minutes=5))
    engine = EngineRunner(adapter, FixedSlTpPolicy(stop_pct=.01, target_pct=.02), EngineConfig(figi='TEST'))
    execution = bars(10, 5)
    first = engine.run(execution)
    assert len(first.trades) == 1
    assert first.trades[0].entry_time == T + timedelta(minutes=30)
    assert engine.run(execution).trades == first.trades
