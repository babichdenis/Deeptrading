from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Mapping, Sequence


@dataclass(frozen=True)
class EquityPosition:
    figi: str
    side: str
    shares: int
    entry_price: float


@dataclass(frozen=True)
class EquityMark:
    price: float
    available_at: datetime


@dataclass(frozen=True)
class EquityPoint:
    as_of: datetime
    equity: float | None
    unrealized_pnl: float | None
    gross_exposure: float | None
    net_exposure: float | None
    unknown: tuple[str, ...]


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Equity timestamps must be timezone-aware')


def mark_paper_equity(*, as_of: datetime, cash: float, positions: Sequence[EquityPosition],
                      marks: Mapping[str, EquityMark], max_mark_age: timedelta,
                      accrued_costs: float | None) -> EquityPoint:
    """Cash already includes realized PnL and paid fees; accrued_costs excludes paid fees."""
    _aware(as_of)
    if not isfinite(cash) or max_mark_age < timedelta(0):
        raise ValueError('Invalid cash or quote age limit')
    if accrued_costs is not None and (not isfinite(accrued_costs) or accrued_costs < 0):
        raise ValueError('Invalid accrued costs')
    seen = set()
    unknown = [] if accrued_costs is not None else ['accrued_costs']
    unrealized = gross = net = 0.0
    for position in positions:
        if position.figi in seen:
            raise ValueError('Duplicate position FIGI')
        seen.add(position.figi)
        if position.side not in ('BUY', 'SELL') or type(position.shares) is not int or position.shares <= 0:
            raise ValueError('Invalid side or share quantity')
        if not isfinite(position.entry_price) or position.entry_price <= 0:
            raise ValueError('Invalid entry price')
        mark = marks.get(position.figi)
        if mark is None:
            unknown.append(position.figi + ':missing_mark')
            continue
        _aware(mark.available_at)
        if mark.available_at > as_of:
            unknown.append(position.figi + ':future_mark')
            continue
        if as_of - mark.available_at > max_mark_age:
            unknown.append(position.figi + ':stale_mark')
            continue
        if not isfinite(mark.price) or mark.price <= 0:
            unknown.append(position.figi + ':invalid_mark')
            continue
        direction = 1 if position.side == 'BUY' else -1
        unrealized += (mark.price - position.entry_price) * position.shares * direction
        gross += mark.price * position.shares
        net += mark.price * position.shares * direction
    if unknown:
        return EquityPoint(as_of, None, None, None, None, tuple(unknown))
    return EquityPoint(as_of, cash + unrealized - accrued_costs, unrealized, gross, net, ())


def observed_max_drawdown(points: Sequence[EquityPoint]) -> float | None:
    """Drawdown of supplied observations only; incomplete valuations remain unknown."""
    if not points or any(point.equity is None for point in points):
        return None
    peak = points[0].equity
    if peak <= 0:
        raise ValueError('Initial equity must be positive')
    worst = 0.0
    previous = None
    for point in points:
        _aware(point.as_of)
        if previous is not None and point.as_of < previous:
            raise ValueError('Equity observations must be chronological')
        previous = point.as_of
        if not isfinite(point.equity):
            raise ValueError('Invalid equity')
        peak = max(peak, point.equity)
        worst = max(worst, (peak - point.equity) / peak)
    return worst
