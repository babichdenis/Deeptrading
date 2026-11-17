from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


@dataclass(frozen=True)
class LongEntryIntent:
    figi: str
    signal_ts: datetime
    signal_high: float
    signal_low: float
    timeframe: timedelta = timedelta(minutes=5)

    def __post_init__(self):
        if self.signal_ts.utcoffset() is None:
            raise ValueError('Aware signal timestamp required')
        if self.timeframe <= timedelta(0):
            raise ValueError('Positive timeframe required')
        if not all(isfinite(x) and x > 0 for x in (self.signal_high, self.signal_low)) or self.signal_high < self.signal_low:
            raise ValueError('Invalid signal range')

    @property
    def available_at(self):
        return self.signal_ts + self.timeframe

    @property
    def confirmation_at(self):
        return self.signal_ts + 2 * self.timeframe


@dataclass(frozen=True)
class EntryConfirmation:
    state: str
    reason: str


def evaluate_long_entry(intent, confirmation_bar, as_of, sell_veto=False):
    if as_of.utcoffset() is None:
        raise ValueError('Aware evaluation timestamp required')
    if as_of < intent.available_at:
        return EntryConfirmation('WAIT', 'signal_not_closed')
    if sell_veto:
        return EntryConfirmation('CANCEL', 'current_sell_quorum')
    if as_of < intent.confirmation_at:
        return EntryConfirmation('WAIT', 'confirmation_not_closed')
    if as_of > intent.confirmation_at:
        return EntryConfirmation('CANCEL', 'expired')
    if confirmation_bar is None or confirmation_bar.ts != intent.available_at:
        return EntryConfirmation('CANCEL', 'missing_exact_confirmation')
    if confirmation_bar.low < intent.signal_low:
        return EntryConfirmation('CANCEL', 'signal_low_broken')
    if confirmation_bar.close <= intent.signal_high:
        return EntryConfirmation('CANCEL', 'no_close_above_signal_high')
    return EntryConfirmation('CONFIRM', 'next_bar_confirmed')
