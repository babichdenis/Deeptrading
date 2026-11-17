from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Callable


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Aware timestamp required')


@dataclass(frozen=True)
class EntryIntent:
    intent_id: str
    group_id: str
    figi: str
    side: str
    shares: int
    trigger_price: float
    created_at: datetime
    expires_at: datetime
    status: str = 'waiting'
    confirmed_at: datetime | None = None
    observed_until: datetime | None = None
    reason: str = ''


@dataclass(frozen=True)
class EntryDispatch:
    intent_id: str
    figi: str
    side: str
    shares: int
    at: datetime
    reference_price: float


class ConditionalEntryBook:
    """Serial bot-held intents; confirmation is bar-close, dispatch is a later bar open.

    Dispatch is not a fill. The caller must persist intent state before broker I/O,
    reconcile submitted orders and apply only confirmed fills to the position book.
    """

    def __init__(self):
        self._intents: dict[str, EntryIntent] = {}
        self._closed_groups: set[str] = set()

    def get(self, intent_id: str) -> EntryIntent:
        return self._intents[intent_id]

    def create(self, *, intent_id: str, group_id: str, figi: str, side: str,
               lots: int, lot_size: int, anchor_price: float, confirmation_fraction: float,
               created_at: datetime, expires_at: datetime) -> EntryIntent:
        _aware(created_at)
        _aware(expires_at)
        if not all(isinstance(value, str) and value for value in (intent_id, group_id, figi)):
            raise ValueError('Intent/group/FIGI required')
        if intent_id in self._intents or group_id in self._closed_groups:
            raise ValueError('Duplicate intent or closed position group')
        if side not in ('BUY', 'SELL') or expires_at <= created_at:
            raise ValueError('Invalid direction or lifetime')
        if any(type(value) is not int or value <= 0 for value in (lots, lot_size)):
            raise ValueError('Positive integer lots and lot size required')
        if not isfinite(anchor_price) or anchor_price <= 0:
            raise ValueError('Invalid anchor')
        if not isfinite(confirmation_fraction) or not 0 <= confirmation_fraction < 1:
            raise ValueError('Invalid confirmation fraction')
        trigger = anchor_price * (1 + confirmation_fraction * (1 if side == 'BUY' else -1))
        if not isfinite(trigger) or trigger <= 0:
            raise ValueError('Invalid trigger')
        intent = EntryIntent(intent_id, group_id, figi, side, lots * lot_size,
                             trigger, created_at, expires_at)
        self._intents[intent_id] = intent
        return intent

    def observe_close(self, intent_id: str, *, bar_start: datetime, timeframe: timedelta,
                      close: float, as_of: datetime, thesis_valid: bool) -> EntryIntent:
        intent = self.get(intent_id)
        _aware(bar_start)
        _aware(as_of)
        if timeframe <= timedelta(0) or not isfinite(close) or close <= 0:
            raise ValueError('Invalid closed bar')
        if type(thesis_valid) is not bool:
            raise ValueError('Explicit thesis validity required')
        closed_at = bar_start + timeframe
        if closed_at > as_of:
            raise ValueError('Future bar is not visible')
        if intent.status not in ('waiting', 'confirmed'):
            return intent
        if intent.observed_until is not None and closed_at <= intent.observed_until:
            raise ValueError('Duplicate or out-of-order observation')
        if as_of >= intent.expires_at:
            updated = replace(intent, status='expired', reason='ttl')
        elif not thesis_valid:
            updated = replace(intent, status='cancelled', reason='thesis_invalid')
        elif bar_start < intent.created_at:
            return intent
        else:
            reached = close >= intent.trigger_price if intent.side == 'BUY' else close <= intent.trigger_price
            updated = replace(intent, observed_until=closed_at)
            if intent.status == 'waiting' and reached:
                updated = replace(updated, status='confirmed', confirmed_at=as_of)
        self._intents[intent_id] = updated
        return updated

    def dispatch_at_open(self, intent_id: str, *, at: datetime, open_price: float,
                         approve_and_reserve: Callable[[EntryDispatch], bool],
                         shares: int | None = None) -> EntryDispatch | None:
        intent = self.get(intent_id)
        _aware(at)
        if not isfinite(open_price) or open_price <= 0:
            raise ValueError('Invalid executable reference price')
        requested_shares = intent.shares if shares is None else shares
        if type(requested_shares) is not int or not 0 < requested_shares <= intent.shares:
            raise ValueError('Dispatch shares must not exceed intent size')
        if intent.status != 'confirmed':
            return None
        if at < max(intent.confirmed_at, intent.observed_until):
            raise ValueError('Cannot execute before confirmation availability')
        if at >= intent.expires_at:
            self._intents[intent_id] = replace(intent, status='expired', reason='ttl')
            return None
        dispatch = EntryDispatch(intent.intent_id, intent.figi, intent.side,
                                 requested_shares, at, open_price)
        self._intents[intent_id] = replace(intent, status='checking')
        try:
            approved = approve_and_reserve(dispatch)
        except Exception:
            self._intents[intent_id] = replace(intent, status='reconciliation', reason='reservation_error')
            raise
        if type(approved) is not bool:
            self._intents[intent_id] = replace(intent, status='reconciliation', reason='reservation_unknown')
            return None
        if intent.group_id in self._closed_groups:
            self._intents[intent_id] = replace(intent, status='reconciliation', reason='cancel_during_reservation')
            return None
        if not approved:
            self._intents[intent_id] = replace(intent, status='rejected', reason='risk_or_capital')
            return None
        self._intents[intent_id] = replace(intent, status='submitted')
        return dispatch

    def cancel_group(self, group_id: str) -> tuple[str, ...]:
        """Invalidate pending adds; return in-flight IDs requiring broker reconciliation."""
        self._closed_groups.add(group_id)
        outstanding = []
        for key, intent in tuple(self._intents.items()):
            if intent.group_id != group_id:
                continue
            if intent.status in ('waiting', 'confirmed'):
                self._intents[key] = replace(intent, status='cancelled', reason='group_closed')
            elif intent.status in ('checking', 'submitted', 'reconciliation'):
                outstanding.append(key)
        return tuple(outstanding)
