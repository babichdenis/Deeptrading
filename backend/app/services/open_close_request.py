from dataclasses import dataclass
from datetime import datetime, timezone
import json
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.models.broker_order_intent import BrokerOrderIntent
from app.services.open_order_intent import OpenIntent
from app.services.broker_order_journal import CloseIntent


@dataclass(frozen=True)
class CloseAfterOpen:
    reason: str
    requested_at: datetime

    def validate(self):
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 64:
            raise ValueError('Explicit close reason required')
        if not isinstance(self.requested_at, datetime) or self.requested_at.utcoffset() is None:
            raise ValueError('Aware close request timestamp required')

    def to_payload(self):
        self.validate()
        return dict(version=1, reason=self.reason, requested_at=self.requested_at.astimezone(timezone.utc).isoformat())

    @classmethod
    def from_payload(cls, value):
        if (not isinstance(value, dict) or set(value) != {'version', 'reason', 'requested_at'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['requested_at'], str)):
            raise ValueError('Invalid durable close-after-open request')
        result = cls(value['reason'], datetime.fromisoformat(value['requested_at']))
        result.validate()
        return result


@dataclass(frozen=True)
class PendingCloseAfterOpen:
    intent: OpenIntent
    request: CloseAfterOpen

    @property
    def close_client_order_id(self):
        identity = json.dumps(['deeptrading.close-after-open.v1', self.intent.mode,
            self.intent.broker_account_id, self.intent.client_order_id], separators=(',', ':'))
        return str(uuid5(NAMESPACE_URL, identity))


@dataclass(frozen=True)
class CloseRemainder:
    closed_units: int
    remaining_units: int
    next_request_id: str | None
    complete: bool


def remainder_request_id(pending, closed_units):
    if closed_units == 0:
        return pending.close_client_order_id
    return str(uuid5(NAMESPACE_URL, json.dumps(
        ['deeptrading.close-remainder.v1', pending.close_client_order_id, closed_units], separators=(',', ':'))))


async def close_remainder_progress(db, opening, pending):
    intent = pending.intent
    if OpenIntent.from_record(opening) != intent:
        raise ValueError('Opening identity changed during exit recovery')
    units = opening.accounted_units
    if opening.unresolved_figi is not None:
        return CloseRemainder(0, units, None, False)
    if opening.observed_units == units == 0:
        complete = (opening.broker_status in ('CANCELLED', 'REJECTED')
                    and opening.runtime_accounted_units in (None, 0))
        return CloseRemainder(0, 0, None, complete)
    if (units <= 0 or opening.status != 'SETTLED' or opening.broker_status not in ('FILLED', 'CANCELLED')
            or opening.observed_units != units or opening.runtime_accounted_units != units
            or not isinstance(opening.entry_accounting, dict)):
        return CloseRemainder(0, units, None, False)
    closed = 0
    while closed < units:
        request_id = remainder_request_id(pending, closed)
        closing = await db.get(BrokerOrderIntent, (intent.mode, intent.broker_account_id, request_id))
        if closing is None:
            return CloseRemainder(closed, units - closed, request_id, False)
        close = CloseIntent.from_record(closing)
        snapshot = close.position_snapshot
        if ((close.mode, close.broker_account_id, close.client_order_id, close.figi, close.side,
                close.requested_units, close.lot_size)
                != (intent.mode, intent.broker_account_id, request_id, intent.figi,
                    'SELL' if intent.side == 'BUY' else 'BUY', units - closed, intent.lot_size)
                or snapshot is None or snapshot.entry_client_order_id != intent.client_order_id
                or snapshot.local_position_id != opening.entry_accounting.get('local_position_id')):
            raise ValueError('Closing journal is not linked to original opening')
        executed = closing.accounted_units
        if (closing.status != 'SETTLED' or closing.unresolved_figi is not None
                or closing.broker_status not in ('FILLED', 'CANCELLED')
                or closing.observed_units != executed or closing.runtime_accounted_units != executed
                or type(executed) is not int or not 0 < executed <= units - closed
                or executed % intent.lot_size):
            return CloseRemainder(closed, units - closed, None, False)
        if closing.broker_status == 'FILLED' and executed != units - closed:
            raise ValueError('Filled closing did not account for its full quantity')
        closed += executed
    return CloseRemainder(closed, 0, None, True)


async def load_close_after_open(sessions, *, mode: str, broker_account_id: str):
    if mode not in ('live', 'sandbox') or not isinstance(broker_account_id, str) or not broker_account_id.strip():
        raise ValueError('Explicit close recovery scope required')
    async with sessions() as db:
        rows = (await db.scalars(select(BrokerOrderIntent).where(
            BrokerOrderIntent.mode == mode, BrokerOrderIntent.broker_account_id == broker_account_id,
            BrokerOrderIntent.action == 'open', BrokerOrderIntent.close_request.is_not(None)
        ).order_by(BrokerOrderIntent.created_at, BrokerOrderIntent.client_order_id))).all()
        recovered = []
        for row in rows:
            if row.close_request is None:
                continue
            intent = OpenIntent.from_record(row)
            request = CloseAfterOpen.from_payload(row.close_request)
            if request.requested_at < intent.created_at:
                raise ValueError('Close request predates opening')
            pending = PendingCloseAfterOpen(intent, request)
            progress = await close_remainder_progress(db, row, pending)
            if not progress.complete:
                recovered.append(pending)
        return recovered


async def request_close_after_open(sessions, intent: OpenIntent, request: CloseAfterOpen):
    intent.validate()
    request.validate()
    if request.requested_at < intent.created_at:
        raise ValueError('Close request predates opening')
    async with sessions() as db:
        async with db.begin():
            row = await db.scalar(select(BrokerOrderIntent).where(
                BrokerOrderIntent.mode == intent.mode, BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                BrokerOrderIntent.client_order_id == intent.client_order_id).with_for_update())
            if row is None or OpenIntent.from_record(row) != intent:
                raise ValueError('Original opening reservation required')
            if row.close_request is not None:
                return CloseAfterOpen.from_payload(row.close_request)
            if row.unresolved_figi != intent.figi:
                raise ValueError('Opening is already released; use normal close path')
            row.close_request = request.to_payload()
        return request


@dataclass(frozen=True)
class CloseAfterOpenHandoff:
    pending: PendingCloseAfterOpen
    units: int
    local_position_id: int
    entry_price: Decimal
    entry_time: datetime
    close_client_order_id: str
    entry_time_source: str = "broker_history"


async def prepare_close_after_open(sessions, pending: PendingCloseAfterOpen):
    from app.models.sandbox_trade import SandboxTrade

    intent = pending.intent
    intent.validate()
    async with sessions() as db:
        opening = await db.get(BrokerOrderIntent, (intent.mode, intent.broker_account_id, intent.client_order_id))
        if (opening is None or OpenIntent.from_record(opening) != intent
                or CloseAfterOpen.from_payload(opening.close_request) != pending.request):
            raise ValueError('Original exit request changed')
        progress = await close_remainder_progress(db, opening, pending)
        if progress.next_request_id is None:
            return None
        units = progress.remaining_units
        position_id = opening.entry_accounting.get('local_position_id')
        if type(position_id) is not int or position_id <= 0:
            raise ValueError('Original local position required for exit')
        position = await db.get(SandboxTrade, position_id)
        if (position is None or position.exit_time is not None
                or (position.mode, position.broker_account_id, position.figi, position.side, position.qty)
                != (intent.mode, intent.broker_account_id, intent.figi, intent.side, units)):
            raise ValueError('Exit position identity changed')
        metadata = json.loads(position.meta or '{}')
        evidence = opening.entry_accounting['evidence']
        from app.services.sandbox_order_evidence import accounting_totals
        evidence_units, price, entry_time = accounting_totals(evidence)
        if (metadata.get('entry_plan') != intent.entry_payload()
                or metadata.get('entry_fill_evidence') != evidence
                or evidence_units != opening.accounted_units
                or position.entry_price != price.quantize(Decimal('0.000001'))
                or position.entry_time != entry_time):
            raise ValueError('Exit position provenance changed')
        return CloseAfterOpenHandoff(pending, units, position_id, position.entry_price, entry_time, progress.next_request_id,
            "sandbox_order_timestamp" if evidence.get("source") == "sandbox_order_aggregate" else "broker_history")
