from bisect import bisect_left, bisect_right
from datetime import timedelta


class ClosedMinuteActivityGate:
    """Count observed closed minutes, not volume, order-book liquidity or calendar completeness."""

    def __init__(self, minute_bars, *, window_minutes=30, minimum_observations=24):
        if (type(window_minutes) is not int or type(minimum_observations) is not int or
                not 1 <= minimum_observations <= window_minutes):
            raise ValueError('Positive integer observation threshold within window required')
        self.window = timedelta(minutes=window_minutes)
        self.minimum = minimum_observations
        self.times = tuple(bar.ts for bar in minute_bars)
        for index, at in enumerate(self.times):
            if at.tzinfo is None or at.utcoffset() is None or at.timestamp() % 60:
                raise ValueError('Aware minute grid required')
            if index and at <= self.times[index - 1]:
                raise ValueError('Minute timestamps must strictly increase')

    def __call__(self, as_of):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError('Aware decision time required')
        left = bisect_left(self.times, as_of - self.window)
        right = bisect_right(self.times, as_of - timedelta(minutes=1))
        return right - left >= self.minimum
