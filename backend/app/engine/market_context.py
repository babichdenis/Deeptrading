from bisect import bisect_right
from datetime import datetime, timedelta
from math import isfinite

from app.engine.models import DecisionAction, PositionState, Side
from app.engine.policies import SignalPolicy


class MarketContextPolicy(SignalPolicy):
    """Gate new entries using causally available relative and market returns."""

    def __init__(self, ticker, snapshots, *, execution_timeframe, market_mode):
        super().__init__()
        if not isinstance(execution_timeframe, timedelta) or execution_timeframe <= timedelta(0):
            raise ValueError('Execution timeframe must be a positive timedelta')
        if market_mode not in ("opposed_or_flat", "aligned", "any"):
            raise ValueError("Explicit market direction mode required")
        self.market_mode = market_mode
        self.execution_timeframe = execution_timeframe
        self.times = []
        self.features = []
        for snapshot in snapshots:
            at = datetime.fromisoformat(snapshot['as_of'])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError('Context availability must be timezone-aware')
            if self.times and at <= self.times[-1]:
                raise ValueError('Context availability must strictly increase')
            feature = snapshot.get('features', {}).get(ticker, {})
            relative = feature.get('relative_return')
            moex = feature.get('moex_return')
            known = feature.get('valid') is True and all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
                for value in (relative, moex))
            self.times.append(at)
            self.features.append((relative, moex) if known else None)

    def decide(self, signal, state, bars_held):
        if signal is not None and state is PositionState.FLAT:
            if signal.time.tzinfo is None or signal.time.utcoffset() is None:
                raise ValueError('Signal time must be timezone-aware')
            at = signal.time + self.execution_timeframe
            index = bisect_right(self.times, at) - 1
            feature = self.features[index] if index >= 0 else None
            side = 1 if signal.side is Side.BUY else -1
            if feature is None or side * feature[0] <= 0:
                return DecisionAction.NO_SIGNAL, 'RELATIVE_CONTEXT_REJECT'
            market_return = side * feature[1]
            if ((self.market_mode == 'opposed_or_flat' and market_return > 0) or
                    (self.market_mode == 'aligned' and market_return <= 0)):
                return DecisionAction.NO_SIGNAL, 'RELATIVE_CONTEXT_REJECT'
        return super().decide(signal, state, bars_held)
