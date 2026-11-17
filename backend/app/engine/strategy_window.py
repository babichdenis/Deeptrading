class StrategyWindow:
    """Warm the underlying strategy on every bar; expose signals in [start, end)."""

    def __init__(self, strategy, *, start, end):
        if any(at.tzinfo is None or at.utcoffset() is None for at in (start,end)) or start >= end:
            raise ValueError('Ordered aware evaluation interval required')
        self.strategy = strategy
        self.start = start
        self.end = end

    def __getattr__(self, name):
        return getattr(self.strategy, name)

    def on_bar(self, bars):
        signal = self.strategy.on_bar(bars)
        return signal if self.start <= bars[-1].ts < self.end else None
