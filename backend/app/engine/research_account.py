from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from app.engine.staged_position import ConfirmedFill, StagedPositionBook
from app.engine.portfolio_margin import MarginExposure


@dataclass(frozen=True)
class AccountSnapshot:
    at: datetime
    cash: float
    equity: float | None
    gross_exposure: float | None
    missing_marks: tuple[str, ...]
    settlement_cash: float = 0.0
    uncovered_cash: float = 0.0
    short_market_value: float | None = 0.0
    uncovered_value: float | None = 0.0


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    projected_equity: float | None = None
    projected_exposure: float | None = None
    projected_uncovered_value: float | None = None
    projected_margin: float | None = None
    margin_limit: float | None = None


def _time(at):
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('Timezone-aware account clock required')


def _finite(value, *, positive=False):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and (
        value > 0 if positive else value >= 0)


class ResearchAccount:
    """Serial research equity ledger; gross/equity limit is not a broker margin model.

    Cash uses confirmed-fill margin accounting and excludes exchanged principal.
    Quotes must be available at their timestamp; missing/stale marks prohibit entries.
    Approval is not a reservation: the single scheduler must apply the fill immediately.
    Settlement cash and uncovered value assume RUB equities only, with no derivatives,
    FX balances or pending broker orders; they are not a complete broker margin ledger.
    """

    def __init__(self, initial_capital: float, *, max_mark_age: timedelta, margin_policy=None):
        if not _finite(initial_capital, positive=True):
            raise ValueError('Positive finite initial capital required')
        if not isinstance(max_mark_age, timedelta) or max_mark_age < timedelta(0):
            raise ValueError('Nonnegative explicit mark age required')
        self.initial_capital = float(initial_capital)
        self.max_mark_age = max_mark_age
        self.margin_policy = margin_policy
        self._books = {}
        self._marks = {}
        self._events = {}
        self._charges = 0.0
        self._clock = None

    def _check_clock(self, at):
        _time(at)
        if self._clock is not None and at < self._clock:
            raise ValueError('Account clock cannot move backwards')

    @property
    def positions(self):
        return tuple(book.state for book in self._books.values() if book.state.shares)

    def mark(self, figi: str, price: float, *, available_at: datetime):
        self._check_clock(available_at)
        if not figi or not _finite(price, positive=True):
            raise ValueError('Valid instrument and positive mark required')
        previous = self._marks.get(figi)
        value = (available_at, float(price))
        if previous is not None and previous[0] == available_at and previous != value:
            raise ValueError('Conflicting mark at the same availability time')
        self._marks[figi] = value
        self._clock = available_at

    def apply_fill(self, fill: ConfirmedFill):
        previous = self._events.get(fill.execution_id)
        if previous is not None:
            if previous != ('fill', fill):
                raise ValueError('Conflicting global execution ID')
            return self._books[fill.figi].apply(fill)
        self._check_clock(fill.at)
        book = self._books.get(fill.figi)
        if book is None:
            book = StagedPositionBook(fill.figi)
        effect = book.apply(fill)
        self._books[fill.figi] = book
        self._events[fill.execution_id] = ('fill', fill)
        self._clock = fill.at
        return effect

    def charge(self, event_id: str, amount: float, *, at: datetime) -> bool:
        event = ('charge', amount, at)
        previous = self._events.get(event_id)
        if previous is not None:
            if previous != event:
                raise ValueError('Conflicting global execution ID')
            return False
        self._check_clock(at)
        if not isinstance(event_id, str) or not event_id or not _finite(amount):
            raise ValueError('Valid charge ID and nonnegative finite amount required')
        if not isfinite(self._charges + amount):
            raise ValueError('Nonfinite accumulated charges')
        self._charges += amount
        self._events[event_id] = event
        self._clock = at
        return True

    def _price(self, figi, at):
        mark = self._marks.get(figi)
        if mark is None or not timedelta(0) <= at - mark[0] <= self.max_mark_age:
            return None
        return mark[1]

    def mark_age_seconds(self, figi, *, at):
        mark = self._marks.get(figi)
        return (at - mark[0]).total_seconds() if mark is not None and mark[0] <= at else None

    def snapshot(self, *, at: datetime, carry_marks: bool = False) -> AccountSnapshot:
        self._check_clock(at)
        cash = self.initial_capital - self._charges + sum(book.state.cash_delta for book in self._books.values())
        equity = cash
        exposure = 0.0
        signed_principal = 0.0
        short_value = 0.0
        unknown_short = False
        missing = []
        for figi, book in self._books.items():
            state = book.state
            if not state.shares:
                continue
            side = 1 if state.side == 'BUY' else -1
            signed_principal += side * state.shares * state.average_entry
            price = self._price(figi, at)
            if price is None and carry_marks:
                mark = self._marks.get(figi)
                if mark is not None and mark[0] <= at:
                    price = mark[1]
            if price is None:
                missing.append(figi)
                unknown_short = unknown_short or state.side == 'SELL'
                continue
            equity += side * state.shares * (price - state.average_entry)
            exposure += state.shares * price
            if state.side == 'SELL':
                short_value += state.shares * price
        settlement_cash = cash - signed_principal
        uncovered_cash = max(0.0, -settlement_cash)
        uncovered = uncovered_cash + short_value
        if not all(isfinite(value) for value in (cash, equity, exposure, settlement_cash, uncovered)):
            raise ValueError('Nonfinite account valuation')
        return AccountSnapshot(at, cash, None if missing else equity,
                               None if missing else exposure, tuple(sorted(missing)),
                               settlement_cash, uncovered_cash,
                               None if unknown_short else short_value,
                               None if unknown_short else uncovered)

    def can_increase(self, fill: ConfirmedFill, *, gross_equity_limit: float,
                     allow_borrowing: bool | None = True, borrowing_policy=None) -> Admission:
        self._check_clock(fill.at)
        if allow_borrowing is not None and type(allow_borrowing) is not bool:
            raise ValueError('Borrowing approval must be boolean or unknown')
        if (fill.intent != 'increase' or fill.side not in ('BUY', 'SELL') or
                type(fill.shares) is not int or fill.shares <= 0 or
                not _finite(fill.price, positive=True) or not _finite(fill.commission) or
                not _finite(gross_equity_limit, positive=True) or
                not isinstance(fill.execution_id, str) or not fill.execution_id or
                not isinstance(fill.figi, str) or not fill.figi):
            return Admission(False, 'INVALID_PROPOSAL')
        if fill.execution_id in self._events:
            return Admission(False, 'EXECUTION_ALREADY_SEEN')
        state = self._books[fill.figi].state if fill.figi in self._books else None
        if state is not None and state.shares and state.side != fill.side:
            return Admission(False, 'OPPOSITE_POSITION')
        snapshot = self.snapshot(at=fill.at)
        price = self._price(fill.figi, fill.at)
        if snapshot.equity is None or price is None:
            return Admission(False, 'UNKNOWN_MARK')
        side = 1 if fill.side == 'BUY' else -1
        equity = snapshot.equity - fill.commission + side * fill.shares * (price - fill.price)
        exposure = snapshot.gross_exposure + fill.shares * price
        settlement_cash = snapshot.settlement_cash - side * fill.shares * fill.price - fill.commission
        uncovered = max(0.0, -settlement_cash) + snapshot.short_market_value
        if fill.side == 'SELL':
            uncovered += fill.shares * price
        if not all(isfinite(value) for value in (equity, exposure, uncovered)) or equity <= 0:
            return Admission(False, 'NONPOSITIVE_EQUITY')
        if allow_borrowing is not True and uncovered > snapshot.uncovered_value:
            return Admission(False, 'BORROWING_NOT_APPROVED', equity, exposure, uncovered)
        if borrowing_policy is not None and uncovered > snapshot.uncovered_value:
            permission = borrowing_policy.evaluate(figi=fill.figi, side=fill.side, at=fill.at)
            if permission.allowed is not True:
                return Admission(False, permission.reason, equity, exposure, uncovered)
        if exposure > gross_equity_limit * equity:
            return Admission(False, 'GROSS_EQUITY_LIMIT', equity, exposure, uncovered)
        if self.margin_policy is not None:
            positions = tuple(MarginExposure(position.figi, position.side,
                              position.shares * self._price(position.figi, fill.at))
                              for position in self.positions)
            decision = self.margin_policy.evaluate(at=fill.at, positions=positions,
                proposal=MarginExposure(fill.figi, fill.side, fill.shares * price),
                projected_liquid=equity)
            if decision.allowed is not True:
                return Admission(False, decision.reason, equity, exposure, uncovered,
                                 decision.projected_margin, decision.limit)
            return Admission(True, 'ACCEPT', equity, exposure, uncovered,
                             decision.projected_margin, decision.limit)
        return Admission(True, 'ACCEPT', equity, exposure, uncovered)
