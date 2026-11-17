from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class TrendObservation:
    bars: int
    closed_at: datetime | None
    valid: bool
    normalized_slope: float | None


def entry_context_rejection(*, signal_bars: int, member: bool, as_of: datetime,
                            trends: Mapping[int, TrendObservation], require_trends: bool = True,
                            side: str = 'BUY'):
    if side not in ('BUY', 'SELL'):
        raise ValueError('Explicit BUY or SELL direction required')
    if as_of.utcoffset() is None:
        raise ValueError('Aware decision time required')
    if signal_bars < 120:
        return 'signal_warmup'
    if not member:
        return 'not_member'
    if not require_trends:
        return None
    for minutes in (15, 60):
        observation = trends.get(minutes)
        if observation is None or observation.bars < 20 or observation.closed_at is None:
            return 'trend_missing_or_stale'
        if observation.closed_at.utcoffset() is None:
            raise ValueError('Aware trend close time required')
        age = as_of - observation.closed_at
        if age < timedelta(0) or age > timedelta(minutes=minutes):
            return 'trend_missing_or_stale'
    direction = 1 if side == 'BUY' else -1
    if any(not trends[minutes].valid or trends[minutes].normalized_slope is None or not isfinite(trends[minutes].normalized_slope) or direction * trends[minutes].normalized_slope <= 0 for minutes in (15, 60)):
        return 'trend_gate'
    return None
