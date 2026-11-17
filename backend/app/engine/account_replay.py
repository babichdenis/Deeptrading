from dataclasses import dataclass, field
from datetime import timedelta
from math import floor, isclose, isfinite

from app.engine.models import ExitReason, Side
from app.engine.research_account import ResearchAccount
from app.engine.runner import EngineRunner
from app.engine.staged_position import ConfirmedFill
from app.engine.views import CandleWindow


MINUTE = timedelta(minutes=1)


@dataclass
class ReplayInstrument:
    candles: object
    strategy: object
    exit_policy: object
    config: object
    policy: object = None
    entry_gate: object = None
    equity_fraction: float | None = None
    lot_size: int = 1
    borrowing_gate: object = None
    borrowing_policy: object = None
    entry_budget: float | None = None
    stop_risk_budget: float | None = None
    short_entry_gate: object = None


@dataclass
class AccountReplayResult:
    ledgers: dict
    equity_curve: list
    rejections: list
    final_equity: float
    max_drawdown: float | None
    known_prefix_max_drawdown: float
    unknown_equity_points: int
    sizing_decisions: list = field(default_factory=list)
    funding_charges: list = field(default_factory=list)
    carried_marks: list = field(default_factory=list)


class _AccountRunner(EngineRunner):
    def __init__(self, spec, account, gross_limit, deferred, rejections, sizing_decisions, max_positions):
        super().__init__(spec.strategy, spec.exit_policy, spec.config)
        if spec.policy is not None:
            self.policy = spec.policy
        self.account = account
        self.gross_limit = gross_limit
        self.deferred = deferred
        self.rejections = rejections
        self.entry_gate = spec.entry_gate
        self.short_entry_gate = spec.short_entry_gate
        self.entry_budget = spec.entry_budget
        self.stop_risk_budget = spec.stop_risk_budget
        self.max_positions = max_positions
        self.equity_fraction = spec.equity_fraction
        self.lot_size = spec.lot_size
        self.sizing_decisions = sizing_decisions
        self.borrowing_gate = spec.borrowing_gate
        self.borrowing_policy = spec.borrowing_policy

    def _open(self, index, bar, candles, signal, position, ledger):
        if position is not None:
            raise ValueError('Shared replay requires flat state before new entry')
        if self.entry_gate is not None and self.entry_gate(bar.ts) is not True:
            self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': 'ENTRY_GATE_REJECT'})
            ledger.log(index, bar.ts, 'ACCOUNT_REJECT', 'ENTRY_GATE_REJECT')
            return None
        if self.max_positions is not None and len(self.account.positions) >= self.max_positions:
            self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': 'MAX_POSITIONS'})
            ledger.log(index, bar.ts, 'ACCOUNT_REJECT', 'MAX_POSITIONS')
            return None
        if signal.side is Side.SELL and self.short_entry_gate is not None:
            reason = self.short_entry_gate(bar.ts)
            if reason is not None:
                self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': reason})
                ledger.log(index, bar.ts, 'ACCOUNT_REJECT', reason)
                return None
        price = self.cfg.cost_model.fill_price(bar.open, signal.side)
        allow_borrowing = True if self.borrowing_gate is None else self.borrowing_gate(bar.ts) is True
        qty = self.cfg.qty
        if self.equity_fraction is not None or self.entry_budget is not None:
            snapshot = self.account.snapshot(at=bar.ts)
            if snapshot.equity is None or snapshot.equity <= 0:
                self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': 'SIZING_EQUITY_UNKNOWN_OR_NONPOSITIVE'})
                ledger.log(index, bar.ts, 'ACCOUNT_REJECT', 'SIZING_EQUITY_UNKNOWN_OR_NONPOSITIVE')
                return None
            budget = self.entry_budget if self.entry_budget is not None else snapshot.equity * self.equity_fraction
            desired_lots = floor(budget / (price * self.lot_size))
            if self.stop_risk_budget is not None:
                plan = self.exit_policy.plan_entry(signal.side, price, CandleWindow(candles, 0, index))
                stop = plan.stop_loss
                direction = 1 if signal.side is Side.BUY else -1
                if stop is None or not isfinite(stop) or stop <= 0 or direction * (price - stop) <= 0:
                    raise ValueError('Risk sizing requires a valid protective stop')
                exit_side = Side.SELL if signal.side is Side.BUY else Side.BUY
                stop_fill = self.cfg.cost_model.fill_price(stop, exit_side)
                risk_per_lot = self.lot_size * direction * (price - stop_fill)
                risk_per_lot += self.cfg.cost_model.commission(self.lot_size * price)
                risk_per_lot += self.cfg.cost_model.commission(self.lot_size * stop_fill)
                desired_lots = min(desired_lots, floor(self.stop_risk_budget / risk_per_lot))
            low, high = 0, desired_lots
            while low < high:
                candidate = (low + high + 1) // 2
                shares = candidate * self.lot_size
                fee = self.cfg.cost_model.commission(price * shares)
                proposal = ConfirmedFill(f'{self.cfg.figi}:entry:{index}', self.cfg.figi,
                    signal.side.value, shares, price, fee, bar.ts, 'increase')
                if self.account.can_increase(proposal, gross_equity_limit=self.gross_limit,
                                             allow_borrowing=allow_borrowing,
                                             borrowing_policy=self.borrowing_policy).allowed:
                    low = candidate
                else:
                    high = candidate - 1
            qty = low * self.lot_size
            self.sizing_decisions.append({'figi': self.cfg.figi, 'at': bar.ts,
                'equity_before': snapshot.equity, 'gross_before': snapshot.gross_exposure,
                'equity_fraction': self.equity_fraction, 'lot_size': self.lot_size,
                'fill_price': price, 'desired_shares': desired_lots * self.lot_size, 'accepted_shares': qty,
                'borrowing_approved': allow_borrowing})
            if not qty:
                self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': 'NO_WHOLE_LOT_CAPACITY'})
                ledger.log(index, bar.ts, 'ACCOUNT_REJECT', 'NO_WHOLE_LOT_CAPACITY')
                return None
        fee = self.cfg.cost_model.commission(price * qty)
        proposal = ConfirmedFill(f'{self.cfg.figi}:entry:{index}', self.cfg.figi,
            signal.side.value, qty, price, fee, bar.ts, 'increase')
        decision = self.account.can_increase(proposal, gross_equity_limit=self.gross_limit,
                                            allow_borrowing=allow_borrowing,
                                            borrowing_policy=self.borrowing_policy)
        if not decision.allowed:
            self.rejections.append({'figi': self.cfg.figi, 'at': bar.ts, 'reason': decision.reason})
            ledger.log(index, bar.ts, 'ACCOUNT_REJECT', decision.reason)
            return None
        original_qty = self.cfg.qty
        try:
            self.cfg.qty = qty
            opened = super()._open(index, bar, candles, signal, position, ledger)
        finally:
            self.cfg.qty = original_qty
        assert isclose(opened.entry_price, price) and isclose(opened.entry_commission, fee)
        self.account.apply_fill(proposal)
        return opened

    def _close(self, index, ts, position, base_price, reason, ledger):
        if position is None:
            return None
        side = Side.SELL if position.state.value == 'LONG' else Side.BUY
        price = self.cfg.cost_model.fill_price(base_price, side)
        fee = self.cfg.cost_model.commission(price * position.qty)
        fill = ConfirmedFill(f'{self.cfg.figi}:exit:{index}', self.cfg.figi,
                             side.value, position.qty, price, fee, ts + MINUTE, 'reduce')
        result = super()._close(index, ts, position, base_price, reason, ledger)
        self.deferred.append(fill)
        return result


def run_shared_account(instruments, *, initial_capital, gross_equity_limit, max_mark_age=MINUTE,
                       funding_policy=None, margin_policy=None, max_positions=None, report_carried_marks=False):
    """Replay canonical minute engines against one serial admission/valuation account.

    Conservatively release all exit proceeds at bar close, including open-price exits.
    At each boundary current opens supersede prior closes before any new admission.
    Sorted FIGI is the explicit simultaneous-entry priority. Optional funding policy
    debits before admissions; missing funding evidence aborts instead of inventing fees.
    Broker maintenance margin is not simulated; stale marks produce unknown equity.
    """
    if isinstance(gross_equity_limit, bool) or not isinstance(gross_equity_limit, (int, float)) or not isfinite(gross_equity_limit) or gross_equity_limit <= 0:
        raise ValueError('Positive finite gross/equity limit required')
    if max_positions is not None and (type(max_positions) is not int or max_positions <= 0):
        raise ValueError('Positive integer position limit required')
    account = ResearchAccount(initial_capital, max_mark_age=max_mark_age, margin_policy=margin_policy)
    specs = {}
    for spec in instruments:
        figi = spec.config.figi
        if figi in specs:
            raise ValueError('One engine per FIGI required')
        if spec.config.partial_r > 0 or spec.config.signal_policy.confirm_flip:
            raise ValueError('Partial exits and flips require separate shared-account integration')
        if type(spec.config.qty) is not int or spec.config.qty <= 0:
            raise ValueError('Positive integer share count required')
        if type(spec.lot_size) is not int or spec.lot_size <= 0:
            raise ValueError('Positive integer lot size required')
        if spec.equity_fraction is not None and (isinstance(spec.equity_fraction, bool) or
                not isinstance(spec.equity_fraction, (int, float)) or not isfinite(spec.equity_fraction) or
                not 0 < spec.equity_fraction <= 1):
            raise ValueError('Equity sizing fraction must be in (0,1]')
        if spec.entry_budget is not None and (spec.equity_fraction is not None or
                isinstance(spec.entry_budget, bool) or not isfinite(spec.entry_budget) or spec.entry_budget <= 0):
            raise ValueError('Fixed entry budget must be positive and cannot be combined with equity sizing')
        if spec.stop_risk_budget is not None and (
                isinstance(spec.stop_risk_budget, bool) or not isfinite(spec.stop_risk_budget)
                or spec.stop_risk_budget <= 0
                or (spec.entry_budget is None and spec.equity_fraction is None)):
            raise ValueError('Positive stop risk budget requires lot sizing')
        EngineRunner._validate_candles(spec.candles)
        for bar in spec.candles:
            if bar.ts.tzinfo is None or bar.ts.utcoffset() is None or bar.ts.timestamp() % 60:
                raise ValueError('Timezone-aware minute grid required')
        if spec.candles:
            specs[figi] = spec
    if not specs:
        return AccountReplayResult({}, [], [], float(initial_capital), 0.0, 0.0, 0)
    deferred = []
    rejections = []
    sizing_decisions = []
    funding_charges = []
    runners, states, indices = {}, {}, {}
    for figi, spec in sorted(specs.items()):
        reset = getattr(spec.strategy, 'reset', None)
        if reset is not None:
            reset()
        runner = _AccountRunner(spec, account, gross_equity_limit, deferred, rejections, sizing_decisions, max_positions)
        state = runner._new_state(spec.candles)
        state.tf_minutes = 1
        state.session_active = runner.session is not None
        runners[figi], states[figi], indices[figi] = runner, state, 0
    at = min(spec.candles[0].ts for spec in specs.values())
    end = max(spec.candles[-1].ts for spec in specs.values()) + MINUTE
    quotes = {}
    curve = []
    carried_marks = []
    peak = float(initial_capital)
    prefix_drawdown = 0.0
    unknown_count = 0
    while at <= end:
        for fill in deferred:
            assert fill.at == at
            account.apply_fill(fill)
        deferred.clear()
        current = []
        for figi, spec in sorted(specs.items()):
            index = indices[figi]
            if index < len(spec.candles) and spec.candles[index].ts == at:
                current.append((figi, index))
                quotes[figi] = spec.candles[index].open
        for figi, price in sorted(quotes.items()):
            account.mark(figi, price, available_at=at)
        quotes.clear()
        if funding_policy is not None:
            assessment = funding_policy.evaluate(at, account)
            if assessment is not None:
                if assessment.amount is None:
                    raise ValueError(f'Unknown funding at {at.isoformat()}: {assessment.reason}')
                account.charge(f'funding:{assessment.billing_date.isoformat()}', assessment.amount, at=at)
                funding_charges.append(assessment)
        for figi, index in current:
            spec, runner, state = specs[figi], runners[figi], states[figi]
            bar = spec.candles[index]
            runner._body(index, bar, spec.candles, state)
            if index + 1 < len(spec.candles):
                if index >= state.warmup - 1:
                    runner._poll(index, spec.candles, state)
                else:
                    spec.strategy.on_bar(CandleWindow(spec.candles, 0, index + 1))
            elif state.position is not None:
                runner._close(index, at, state.position, bar.close, ExitReason.END_OF_DATA.value, state.ledger)
                state.position = None
            quotes[figi] = bar.close
            indices[figi] += 1
        snapshot = account.snapshot(at=at)
        curve.append(snapshot)
        if snapshot.equity is None:
            unknown_count += 1
            if report_carried_marks:
                carried_marks.append({
                    'at': at,
                    'equity': account.snapshot(at=at, carry_marks=True).equity,
                    'ages_seconds': {figi: account.mark_age_seconds(figi, at=at)
                                     for figi in snapshot.missing_marks},
                })
        elif unknown_count == 0:
            peak = max(peak, snapshot.equity)
            prefix_drawdown = max(prefix_drawdown, (peak - snapshot.equity) / peak)
        at += MINUTE
    assert not deferred
    final = account.snapshot(at=end)
    assert final.gross_exposure == 0 and final.equity is not None
    ledgers = {figi: state.ledger for figi, state in states.items()}
    net = sum(trade.net_pnl for ledger in ledgers.values() for trade in ledger.trades)
    charges = sum(charge.amount for charge in funding_charges)
    assert isclose(final.equity, initial_capital + net - charges, abs_tol=1e-6)
    return AccountReplayResult(ledgers, curve, rejections, final.equity,
                               None if unknown_count else prefix_drawdown, prefix_drawdown, unknown_count,
                               sizing_decisions, funding_charges, carried_marks)
