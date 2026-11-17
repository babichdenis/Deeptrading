from datetime import timedelta
from math import floor, isfinite

from app.engine.conditional_entry import ConditionalEntryBook
from app.engine.conditional_runner import ConditionalEntryRunner
from app.engine.models import DecisionAction, PositionState, Side, Signal
from app.engine.staged_position import ConfirmedFill, StagedPositionBook


class StagedEntryRunner(ConditionalEntryRunner):
    """Two-tranche research execution with a fixed initial stop/target and risk budget.

    Full-size baseline risk includes estimated fees and stop slippage, not gap risk.
    Adds keep the original price levels and never exceed the full-size entry notional.
    """

    def __init__(self, *args, add_confirmation_fraction: float, add_lifetime: timedelta, **kwargs):
        super().__init__(*args, **kwargs)
        if self.cfg.qty < 2 * self.lot_size:
            raise ValueError('Two tranches require at least two lots')
        if not isfinite(add_confirmation_fraction) or not 0 <= add_confirmation_fraction < 1:
            raise ValueError('Invalid add confirmation')
        if add_lifetime <= timedelta(0):
            raise ValueError('Positive add lifetime required')
        self.add_confirmation_fraction = add_confirmation_fraction
        self.add_lifetime = add_lifetime
        self.planned_qty = self.cfg.qty

    def _new_state(self, candles):
        state = super()._new_state(candles)
        self.add_intents = ConditionalEntryBook()
        self.add_events = []
        self._add_key = None
        self.position_book = None
        self.position_books = []
        self._fill_sequence = 0
        return state

    def _fill(self, side, qty, price, commission, at, intent):
        self._fill_sequence += 1
        return self.position_book.apply(ConfirmedFill(str(self._fill_sequence), self.cfg.figi,
            side.value, qty, price, commission, at, intent))

    def _open(self, index, bar, candles, signal, position, ledger):
        self.cfg.qty = (self.planned_qty // self.lot_size // 2) * self.lot_size
        try:
            opened = super()._open(index, bar, candles, signal, position, ledger)
        finally:
            self.cfg.qty = self.planned_qty
        if opened is None:
            return None
        if opened.initial_stop is None:
            raise ValueError('Staged entries require an explicit initial stop')
        self.position_book = StagedPositionBook(self.cfg.figi)
        self.position_books.append(self.position_book)
        self._fill(signal.side, opened.qty, opened.entry_price, opened.entry_commission, bar.ts, 'increase')
        exit_side = Side.SELL if signal.side is Side.BUY else Side.BUY
        stop_fill = self.cfg.cost_model.fill_price(opened.initial_stop, exit_side)
        self._risk_budget = (abs(opened.entry_price - stop_fill) * self.planned_qty
            + self.cfg.cost_model.commission(opened.entry_price * self.planned_qty)
            + self.cfg.cost_model.commission(stop_fill * self.planned_qty))
        self._notional_budget = opened.entry_price * self.planned_qty
        self._add_key = opened.chain_id
        self.add_intents.create(intent_id=self._add_key, group_id=self._add_key, figi=self.cfg.figi,
            side=signal.side.value, lots=(self.planned_qty - opened.qty) // self.lot_size,
            lot_size=self.lot_size, anchor_price=opened.entry_price,
            confirmation_fraction=self.add_confirmation_fraction,
            created_at=bar.ts, expires_at=bar.ts + self.add_lifetime)
        return opened

    def _cancel_add(self):
        if self._add_key is not None:
            self.add_intents.cancel_group(self._add_key)
            self._add_key = None

    def _poll(self, i, candles, st):
        super()._poll(i, candles, st)
        if st.position is None or st.pending_kind in ('exit', 'flip'):
            self._cancel_add()
            return
        if self._add_key is None:
            return
        bar = candles[i]
        side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
        synthetic = Signal('staged_add', side, bar.ts)
        action, _ = self.policy.decide(synthetic, PositionState.FLAT, 0)
        valid = action is DecisionAction.ACCEPT_ENTRY
        if st.session_active:
            valid = valid and self.session.can_enter(bar.ts, st.tf_minutes)[0]
        intent = self.add_intents.observe_close(self._add_key, bar_start=bar.ts,
            timeframe=self.timeframe, close=bar.close, as_of=bar.ts + self.timeframe, thesis_valid=valid)
        if intent.status not in ('waiting', 'confirmed'):
            self._add_key = None

    def _body(self, i, bar, candles, st):
        if self._add_key is not None and st.position is not None:
            intent = self.add_intents.get(self._add_key)
            if intent.status == 'confirmed':
                pos = st.position
                long = pos.state is PositionState.LONG
                stop_hit = bar.open <= pos.initial_stop if long else bar.open >= pos.initial_stop
                target_hit = pos.target is not None and (bar.open >= pos.target if long else bar.open <= pos.target)
                session_ok = not st.session_active or self.session.can_enter(bar.ts, 0)[0]
                if st.session_active and not self.cfg.session_policy.overnight:
                    session_ok = session_ok and self.session.local(bar.ts).date() == st.current_session_date
                if stop_hit or target_hit or not session_ok or st.pending_kind in ('exit', 'flip'):
                    self._cancel_add()
                else:
                    side = Side.BUY if long else Side.SELL
                    exit_side = Side.SELL if long else Side.BUY
                    fill = self.cfg.cost_model.fill_price(bar.open, side)
                    stop_fill = self.cfg.cost_model.fill_price(pos.initial_stop, exit_side)
                    room = self._notional_budget - pos.entry_price * pos.qty
                    qty = min(intent.shares, floor(max(0, room) / (fill * self.lot_size)) * self.lot_size)
                    while qty > 0:
                        average = (pos.entry_price * pos.qty + fill * qty) / (pos.qty + qty)
                        fee = self.cfg.cost_model.commission(fill * qty)
                        risk = max(0, (average - stop_fill) * (1 if long else -1)) * (pos.qty + qty)
                        risk += pos.entry_commission + fee + self.cfg.cost_model.commission(stop_fill * (pos.qty + qty))
                        if risk <= self._risk_budget + 1e-9:
                            break
                        qty -= self.lot_size
                    key = self._add_key
                    self._add_key = None
                    if qty <= 0:
                        self.add_intents.cancel_group(key)
                        self.add_events.append({'at': str(bar.ts), 'result': 'risk_or_notional_reject'})
                    else:
                        dispatched = self.add_intents.dispatch_at_open(key, at=bar.ts,
                            open_price=bar.open, approve_and_reserve=self.approve_and_reserve, shares=qty)
                        if dispatched is not None:
                            self._fill(side, qty, fill, fee, bar.ts, 'increase')
                            pos.qty = self.position_book.state.shares
                            pos.entry_price = self.position_book.state.average_entry
                            pos.entry_commission = self.position_book.state.unallocated_entry_commission
                            pos.entry_slippage += abs(fill - bar.open) * qty
                            self._pos_key = None
                            self.add_events.append({'at': str(bar.ts), 'result': 'filled', 'shares': qty,
                                'planned_risk': risk, 'risk_budget': self._risk_budget,
                                'entry_notional': pos.entry_price * pos.qty, 'notional_budget': self._notional_budget})
                            st.ledger.log(i, bar.ts, 'FILL_ADD', f'qty={qty} price={fill}')
        super()._body(i, bar, candles, st)

    def _close(self, index, ts, position, base_price, reason, ledger):
        if position is not None:
            self._cancel_add()
            side = Side.SELL if position.state is PositionState.LONG else Side.BUY
            price = self.cfg.cost_model.fill_price(base_price, side)
            self._fill(side, position.qty, price, self.cfg.cost_model.commission(price * position.qty), ts, 'reduce')
        return super()._close(index, ts, position, base_price, reason, ledger)

    def _close_partial(self, index, ts, position, base_price, fraction, ledger):
        self._cancel_add()
        qty = min(position.qty, max(self.lot_size,
            floor(position.qty * fraction / self.lot_size) * self.lot_size))
        side = Side.SELL if position.state is PositionState.LONG else Side.BUY
        price = self.cfg.cost_model.fill_price(base_price, side)
        self._fill(side, qty, price, self.cfg.cost_model.commission(price * qty), ts, 'reduce')
        return super()._close_partial(index, ts, position, base_price, qty / position.qty, ledger)
