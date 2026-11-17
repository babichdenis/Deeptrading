from dataclasses import replace
from datetime import timedelta

from app.engine.conditional_entry import ConditionalEntryBook
from app.engine.models import Signal
from app.engine.runner import EngineRunner
from app.engine.views import CandleWindow


class _SignalOnce:
    def __init__(self, strategy, signal):
        self.strategy = strategy
        self.signal = signal

    def on_bar(self, candles):
        return self.signal

    def __getattr__(self, key):
        return getattr(self.strategy, key)


class ConditionalEntryRunner(EngineRunner):
    """Opt-in closed-bar entry confirmation; all fills and exits use EngineRunner.

    None disables the adapter exactly. Enabled mode is single-position research;
    staged additions and broker lifecycle integration are not provided here.
    """

    def __init__(self, strategy, exit_policy, config, *, confirmation_fraction,
                 lifetime: timedelta, timeframe: timedelta, lot_size: int,
                 approve_and_reserve):
        super().__init__(strategy, exit_policy, config)
        if lifetime <= timedelta(0) or timeframe <= timedelta(0):
            raise ValueError('Positive lifetime and timeframe required')
        if type(lot_size) is not int or lot_size <= 0 or config.qty % lot_size:
            raise ValueError('Quantity must be whole lots')
        if confirmation_fraction is not None:
            if not 0 <= confirmation_fraction < 1:
                raise ValueError('Invalid confirmation fraction')
            if config.signal_policy.entry_confirm_bars or config.signal_policy.confirm_flip:
                raise ValueError('Do not combine conditional entries with legacy confirmation or flips')
            if not callable(approve_and_reserve):
                raise ValueError('Explicit reservation callback required')
        self.confirmation_fraction = confirmation_fraction
        self.lifetime = lifetime
        self.timeframe = timeframe
        self.lot_size = lot_size
        self.approve_and_reserve = approve_and_reserve
        self.intents = ConditionalEntryBook()
        self.intent_ids = []
        self._active_intent = None
        self._intent_signal = None

    def _new_state(self, candles):
        state = super()._new_state(candles)
        self.intents = ConditionalEntryBook()
        self.intent_ids = []
        self._active_intent = None
        self._intent_signal = None
        return state

    def _poll(self, i, candles, st):
        if self.confirmation_fraction is None:
            return super()._poll(i, candles, st)
        strategy = self.strategy
        raw = strategy.on_bar(CandleWindow(candles, 0, i + 1))
        bar = candles[i]
        signal = raw
        if st.position is None:
            signal = None
            key = self._active_intent
            if key is not None:
                valid = raw is None or (raw.kind != 'exit' and raw.side == self._intent_signal.side)
                intent = self.intents.observe_close(key, bar_start=bar.ts, timeframe=self.timeframe,
                    close=bar.close, as_of=bar.ts + self.timeframe, thesis_valid=valid)
                if intent.status == 'confirmed':
                    signal = replace(self._intent_signal, time=bar.ts)
                elif intent.status != 'waiting':
                    self._active_intent = None
                    st.ledger.log(i, bar.ts, 'CONDITIONAL_ENTRY', intent.status + ':' + intent.reason)
            if self._active_intent is None and raw is not None and raw.kind == 'entry':
                key = f'{self.cfg.figi}:intent:{len(self.intent_ids) + 1}'
                created = bar.ts + self.timeframe
                self.intents.create(intent_id=key, group_id=key, figi=self.cfg.figi,
                    side=raw.side.value, lots=self.cfg.qty // self.lot_size, lot_size=self.lot_size,
                    anchor_price=bar.close, confirmation_fraction=self.confirmation_fraction,
                    created_at=created, expires_at=created + self.lifetime)
                self.intent_ids.append(key)
                self._active_intent = key
                self._intent_signal = raw
                st.ledger.log(i, bar.ts, 'CONDITIONAL_ENTRY', 'waiting:' + key)
        self.strategy = _SignalOnce(strategy, signal)
        try:
            super()._poll(i, candles, st)
        finally:
            self.strategy = strategy
        if signal is not None and st.position is None and self._active_intent is not None:
            if st.pending_kind != 'entry':
                self.intents.cancel_group(self._active_intent)
                self._active_intent = None

    def _open(self, index, bar, candles, signal: Signal, position, ledger):
        if self.confirmation_fraction is not None:
            key = self._active_intent
            if key is None:
                raise ValueError('Conditional entry without an intent')
            dispatch = self.intents.dispatch_at_open(key, at=bar.ts, open_price=bar.open,
                approve_and_reserve=self.approve_and_reserve, shares=self.cfg.qty)
            self._active_intent = None
            if dispatch is None:
                ledger.log(index, bar.ts, 'CONDITIONAL_ENTRY', self.intents.get(key).status)
                return None
        return super()._open(index, bar, candles, signal, position, ledger)
