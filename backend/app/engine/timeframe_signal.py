from bisect import bisect_right
from dataclasses import replace
from datetime import timedelta

from app.engine.runner import EngineRunner
from app.engine.views import CandleWindow


class TimeframeSignalAdapter:
    """Consume provided complete signal bars; execute on a separate, finer bar clock.

    Source coverage/aggregation is the hub's responsibility. Historical catch-up
    updates indicator state but never replays stale signals after an execution gap.
    """

    def __init__(self, strategy, signal_bars, *, signal_timeframe: timedelta,
                 execution_timeframe: timedelta):
        if execution_timeframe <= timedelta(0) or signal_timeframe < execution_timeframe:
            raise ValueError('Signal timeframe must be at least the positive execution timeframe')
        if signal_timeframe % execution_timeframe != timedelta(0):
            raise ValueError('Signal timeframe must be an integer multiple of execution timeframe')
        self.strategy = strategy
        self.signal_bars = tuple(signal_bars)
        EngineRunner._validate_candles(self.signal_bars)
        for index, bar in enumerate(self.signal_bars):
            if bar.ts.tzinfo is None or bar.ts.utcoffset() is None:
                raise ValueError('Signal bars must have aware timestamps')
            if index and bar.ts - self.signal_bars[index - 1].ts < signal_timeframe:
                raise ValueError('Overlapping signal bars')
        self.signal_timeframe = signal_timeframe
        self.execution_timeframe = execution_timeframe
        self._available = tuple(bar.ts + signal_timeframe for bar in self.signal_bars)
        self.reset()

    @property
    def strategy_id(self):
        return self.strategy.strategy_id

    def warmup_bars(self):
        return 1

    def reset(self):
        reset = getattr(self.strategy, 'reset', None)
        if reset is not None:
            reset()
        self._consumed = 0
        self._last_as_of = None

    def on_bar(self, execution_bars):
        if not execution_bars:
            return None
        current = execution_bars[-1]
        if current.ts.tzinfo is None or current.ts.utcoffset() is None:
            raise ValueError('Execution bars must have aware timestamps')
        as_of = current.ts + self.execution_timeframe
        if self._last_as_of is not None and as_of <= self._last_as_of:
            raise ValueError('Execution clock must advance')
        available_count = bisect_right(self._available, as_of)
        result = None
        while self._consumed < available_count:
            index = self._consumed
            signal = self.strategy.on_bar(CandleWindow(self.signal_bars, 0, index + 1))
            self._consumed += 1
            if self._available[index] == as_of and self._consumed >= self.strategy.warmup_bars():
                result = signal
        self._last_as_of = as_of
        if result is None:
            return None
        return replace(result, time=current.ts,
            features={**result.features, 'source_signal_bar_start': result.time.timestamp(),
                      'source_signal_tf_seconds': self.signal_timeframe.total_seconds()})
