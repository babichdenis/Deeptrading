from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Iterable


@dataclass(frozen=True)
class ConfirmedFill:
    execution_id: str
    figi: str
    side: str
    shares: int
    price: float
    commission: float
    at: datetime
    intent: str


@dataclass(frozen=True)
class StagedPosition:
    figi: str
    side: str | None = None
    shares: int = 0
    average_entry: float = 0.0
    unallocated_entry_commission: float = 0.0
    realized_net: float = 0.0
    cash_delta: float = 0.0


@dataclass(frozen=True)
class FillEffect:
    applied: bool
    cash_delta: float
    closed_shares: int
    gross_pnl: float
    allocated_entry_commission: float
    closed_net_pnl: float


class StagedPositionBook:
    """Confirmed fills in shares; margin-style cash excludes exchanged principal.

    Intent/risk approval, lot conversion and uncertain broker orders belong upstream.
    Rebuild from the durable confirmed-fill journal before resuming after a restart.
    """

    def __init__(self, figi: str):
        if not isinstance(figi, str) or not figi:
            raise ValueError('FIGI required')
        self._state = StagedPosition(figi)
        self._fills: dict[str, ConfirmedFill] = {}

    @property
    def state(self) -> StagedPosition:
        return self._state

    @property
    def fills(self) -> tuple[ConfirmedFill, ...]:
        return tuple(self._fills.values())

    @classmethod
    def rebuild(cls, figi: str, fills: Iterable[ConfirmedFill]):
        book = cls(figi)
        for fill in fills:
            book.apply(fill)
        return book

    def apply(self, fill: ConfirmedFill) -> FillEffect:
        if not isinstance(fill.execution_id, str) or not fill.execution_id:
            raise ValueError('Execution ID required')
        previous = self._fills.get(fill.execution_id)
        if previous is not None:
            if previous != fill:
                raise ValueError('Conflicting execution ID requires reconciliation')
            return FillEffect(False, 0.0, 0, 0.0, 0.0, 0.0)
        if fill.figi != self._state.figi or fill.side not in ('BUY', 'SELL'):
            raise ValueError('Invalid instrument or side')
        if fill.intent not in ('increase', 'reduce'):
            raise ValueError('Explicit increase/reduce intent required')
        if type(fill.shares) is not int or fill.shares <= 0:
            raise ValueError('Positive integer shares required')
        if not isfinite(fill.price) or fill.price <= 0 or not isfinite(fill.commission) or fill.commission < 0:
            raise ValueError('Invalid confirmed price or commission')
        if fill.at.tzinfo is None or fill.at.utcoffset() is None:
            raise ValueError('Aware execution time required')
        if self._fills and fill.at < next(reversed(self._fills.values())).at:
            raise ValueError('Out-of-order execution requires reconciliation')
        state = self._state
        if fill.intent == 'increase':
            if state.shares and state.side != fill.side:
                raise ValueError('Increase cannot reverse an existing position')
            shares = state.shares + fill.shares
            average = (state.average_entry * state.shares + fill.price * fill.shares) / shares
            updated = replace(state, side=fill.side, shares=shares, average_entry=average,
                unallocated_entry_commission=state.unallocated_entry_commission + fill.commission,
                cash_delta=state.cash_delta - fill.commission)
            effect = FillEffect(True, -fill.commission, 0, 0.0, 0.0, 0.0)
        else:
            if not state.shares or state.side == fill.side or fill.shares > state.shares:
                raise ValueError('Reduce must close existing shares without reversal')
            remaining = state.shares - fill.shares
            allocated = (state.unallocated_entry_commission if not remaining else
                         state.unallocated_entry_commission * fill.shares / state.shares)
            direction = 1 if state.side == 'BUY' else -1
            gross = (fill.price - state.average_entry) * fill.shares * direction
            cash = gross - fill.commission
            net = cash - allocated
            updated = replace(state, side=state.side if remaining else None, shares=remaining,
                average_entry=state.average_entry if remaining else 0.0,
                unallocated_entry_commission=state.unallocated_entry_commission - allocated,
                realized_net=state.realized_net + net, cash_delta=state.cash_delta + cash)
            effect = FillEffect(True, cash, fill.shares, gross, allocated, net)
        if not all(isfinite(value) for value in (updated.average_entry,
                updated.unallocated_entry_commission, updated.realized_net, updated.cash_delta)):
            raise ValueError('Nonfinite accounting result')
        self._state = updated
        self._fills[fill.execution_id] = fill
        return effect
