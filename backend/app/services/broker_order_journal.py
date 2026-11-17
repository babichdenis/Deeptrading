from dataclasses import dataclass
from typing import ClassVar
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperTrade
from app.services.close_position_snapshot import ClosePositionSnapshot
from app.services.broker_fill_evidence import OrderFillEvidence
from app.services.close_fill_accounting import build_close_delta


@dataclass(frozen=True)
class OrderIntent:
    action: ClassVar[str] = "close"
    mode: str
    broker_account_id: str
    client_order_id: str
    figi: str
    side: str
    requested_units: int
    lot_size: int
    created_at: datetime

    def validate(self):
        if self.mode not in ('live', 'sandbox') or self.side not in ('BUY', 'SELL'):
            raise ValueError('Invalid order scope or side')
        for value, limit in ((self.broker_account_id, 64), (self.client_order_id, 64), (self.figi, 32)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError('Invalid order identity')
        if (type(self.requested_units) is not int or type(self.lot_size) is not int
                or self.requested_units <= 0 or self.lot_size <= 0
                or self.requested_units % self.lot_size):
            raise ValueError('Whole confirmed lots required')
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError('Aware order timestamp required')

    def entry_payload(self):
        return None


@dataclass(frozen=True)
class CloseIntent(OrderIntent):
    position_snapshot: ClosePositionSnapshot | None = None

    def validate(self):
        super().validate()
        if self.position_snapshot is not None:
            if not isinstance(self.position_snapshot, ClosePositionSnapshot):
                raise ValueError('Typed position snapshot required')
            self.position_snapshot.validate_for(self)

    @classmethod
    def from_record(cls, row):
        if row.action != "close":
            raise ValueError("Opening intent cannot be restored as a close")
        snapshot = ClosePositionSnapshot.from_payload(row.position_snapshot) if row.position_snapshot is not None else None
        result = cls(row.mode, row.broker_account_id, row.client_order_id, row.figi, row.side,
                     row.requested_units, row.lot_size, row.created_at, snapshot)
        result.validate()
        return result


@dataclass(frozen=True)
class CloseSettlement:
    applied_units: int
    accounted_units: int
    remaining_units: int
    released: bool
    trade_id: int | None


class BrokerOrderJournal:
    """A committed reservation is required before submission; recovery never authorizes resend."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def reserve(self, intent: OrderIntent) -> bool:
        intent.validate()
        async with self.sessions() as db:
            db.add(BrokerOrderIntent(
                action=intent.action, entry_plan=intent.entry_payload(),
                mode=intent.mode, broker_account_id=intent.broker_account_id,
                client_order_id=intent.client_order_id, figi=intent.figi,
                unresolved_figi=intent.figi, side=intent.side,
                requested_units=intent.requested_units, lot_size=intent.lot_size,
                created_at=intent.created_at, status='PENDING_RECONCILIATION',
                position_snapshot=intent.position_snapshot.to_payload() if getattr(intent, "position_snapshot", None) else None))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False
        return True

    async def unresolved(self, *, mode: str, broker_account_id: str, action: str = "close"):
        if mode not in ('live', 'sandbox') or not broker_account_id or action not in ('open', 'close'):
            raise ValueError('Explicit broker scope required')
        async with self.sessions() as db:
            rows = (await db.execute(select(BrokerOrderIntent).where(
                BrokerOrderIntent.action == action,
                BrokerOrderIntent.mode == mode,
                BrokerOrderIntent.broker_account_id == broker_account_id,
                BrokerOrderIntent.unresolved_figi.is_not(None),
            ).order_by(BrokerOrderIntent.created_at, BrokerOrderIntent.client_order_id))).scalars().all()
            return rows

    async def load(self, intent: OrderIntent):
        intent.validate()
        async with self.sessions() as db:
            row = await db.get(BrokerOrderIntent, (intent.mode, intent.broker_account_id, intent.client_order_id))
            if row is not None and (row.action != intent.action or row.figi != intent.figi or row.side != intent.side
                    or row.requested_units != intent.requested_units or row.lot_size != intent.lot_size):
                raise ValueError('Stored close identity differs from original request')
            return row

    async def reject_sdk_invalid_id(self, intent: OrderIntent):
        from t_tech.invest.utils import empty_or_uuid
        intent.validate()
        if empty_or_uuid(intent.client_order_id):
            raise ValueError('SDK-valid request cannot be classified as local UUID rejection')
        async with self.sessions() as db:
            row = await db.get(BrokerOrderIntent,
                               (intent.mode, intent.broker_account_id, intent.client_order_id),
                               with_for_update=True)
            if row is None:
                raise ValueError('Original reservation missing')
            if any(getattr(row, key) != getattr(intent, key) for key in
                   ('action', 'figi', 'side', 'requested_units', 'lot_size')):
                raise ValueError('Reservation identity mismatch')
            if row.status == 'LOCAL_REJECTED_INVALID_UUID' and row.unresolved_figi is None:
                return False
            fills = (await db.execute(select(BrokerOrderFill).where(
                BrokerOrderFill.mode == intent.mode,
                BrokerOrderFill.broker_account_id == intent.broker_account_id,
                BrokerOrderFill.client_order_id == intent.client_order_id))).first()
            if (row.status != 'PENDING_RECONCILIATION' or row.broker_order_id is not None
                    or row.broker_status is not None or row.observed_units != 0
                    or row.accounted_units != 0 or row.runtime_accounted_units not in (None, 0)
                    or row.entry_accounting is not None or fills is not None):
                raise ValueError('Reservation contains execution evidence')
            row.status = 'LOCAL_REJECTED_INVALID_UUID'
            row.runtime_accounted_units = 0
            row.unresolved_figi = None
            await db.commit()
            return True

    async def observe(self, intent: CloseIntent, *, broker_order_id: str,
                      broker_status: str, executed_units: int) -> bool:
        """Store validated cumulative broker evidence; nonzero fills still require accounting."""
        intent.validate()
        if (not isinstance(broker_order_id, str) or not broker_order_id.strip()
                or len(broker_order_id) > 128
                or broker_status not in ('NEW', 'PARTIAL_FILLED', 'FILLED', 'CANCELLED', 'REJECTED')
                or type(executed_units) is not int
                or not 0 <= executed_units <= intent.requested_units
                or executed_units % intent.lot_size):
            raise ValueError('Invalid cumulative broker evidence')
        empty_terminal = broker_status in ('CANCELLED', 'REJECTED') and executed_units == 0
        values = dict(broker_order_id=broker_order_id, broker_status=broker_status,
                      observed_units=executed_units,
                      status=broker_status if empty_terminal else 'PENDING_RECONCILIATION')
        if empty_terminal:
            values['unresolved_figi'] = None
        async with self.sessions() as db:
            result = await db.execute(update(BrokerOrderIntent).where(
                BrokerOrderIntent.action == intent.action,
                BrokerOrderIntent.mode == intent.mode,
                BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                BrokerOrderIntent.client_order_id == intent.client_order_id,
                BrokerOrderIntent.figi == intent.figi,
                BrokerOrderIntent.unresolved_figi == intent.figi,
                BrokerOrderIntent.side == intent.side,
                BrokerOrderIntent.requested_units == intent.requested_units,
                BrokerOrderIntent.lot_size == intent.lot_size,
                BrokerOrderIntent.observed_units <= executed_units,
                or_(BrokerOrderIntent.broker_order_id.is_(None),
                    BrokerOrderIntent.broker_order_id == broker_order_id),
            ).values(**values))
            await db.commit()
            if result.rowcount == 1:
                return True
            if not empty_terminal:
                return False
            existing = await db.scalar(select(BrokerOrderIntent).where(
                BrokerOrderIntent.action == intent.action,
                BrokerOrderIntent.mode == intent.mode,
                BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                BrokerOrderIntent.client_order_id == intent.client_order_id,
                BrokerOrderIntent.figi == intent.figi,
                BrokerOrderIntent.side == intent.side,
                BrokerOrderIntent.requested_units == intent.requested_units,
                BrokerOrderIntent.lot_size == intent.lot_size,
                BrokerOrderIntent.broker_order_id == broker_order_id,
                BrokerOrderIntent.broker_status == broker_status,
                BrokerOrderIntent.status == broker_status,
                BrokerOrderIntent.observed_units == 0,
                BrokerOrderIntent.accounted_units == 0,
                BrokerOrderIntent.unresolved_figi.is_(None),
            ))
            return existing is not None


    async def settle(self, intent: CloseIntent, *, broker_order_id: str,
                     cumulative_units: int, trade: PaperTrade | None = None,
                     evidence: OrderFillEvidence | None = None,
                     defer_runtime: bool = False) -> CloseSettlement:
        """Atomically account the unrecorded delta from staged evidence or a validated transient trade."""
        intent.validate()
        if intent.action != "close":
            raise ValueError("Opening intent requires entry accounting")
        if type(defer_runtime) is not bool:
            raise ValueError('Explicit runtime settlement policy required')
        if (not isinstance(broker_order_id, str) or not broker_order_id.strip()
                or type(cumulative_units) is not int or cumulative_units <= 0
                or cumulative_units > intent.requested_units or cumulative_units % intent.lot_size):
            raise ValueError('Invalid settlement evidence')
        async with self.sessions() as db:
            async with db.begin():
                row = await db.scalar(select(BrokerOrderIntent).where(
                    BrokerOrderIntent.action == intent.action,
                    BrokerOrderIntent.mode == intent.mode,
                    BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                    BrokerOrderIntent.client_order_id == intent.client_order_id,
                ).with_for_update())
                if (row is None or row.figi != intent.figi or row.side != intent.side
                        or row.requested_units != intent.requested_units or row.lot_size != intent.lot_size
                        or row.broker_order_id != broker_order_id
                        or row.observed_units != cumulative_units):
                    raise ValueError('Settlement does not match stored broker evidence')
                if defer_runtime and row.runtime_accounted_units is None:
                    if row.unresolved_figi != intent.figi or row.accounted_units != 0:
                        raise ValueError('Cannot infer runtime acknowledgement for previously accounted execution')
                    row.runtime_accounted_units = 0
                execution_evidence = None
                if evidence is not None:
                    if (trade is not None or evidence.broker_order_id != broker_order_id
                            or evidence.units != cumulative_units):
                        raise ValueError('Stage evidence conflicts with supplied settlement')
                    records = (await db.execute(select(BrokerOrderFill).where(
                        BrokerOrderFill.mode == intent.mode,
                        BrokerOrderFill.broker_account_id == intent.broker_account_id,
                        BrokerOrderFill.client_order_id == intent.client_order_id,
                    ).order_by(BrokerOrderFill.cumulative_units))).scalars().all()
                    saved_intent = CloseIntent.from_record(row)
                    from app.services.sandbox_order_evidence import SandboxOrderAggregate
                    from app.services.entry_commission_evidence import linked_entry_commission
                    confirmed_entry_commission = (await linked_entry_commission(db, saved_intent)
                        if isinstance(evidence, SandboxOrderAggregate) else None)
                    trade, execution_evidence = build_close_delta(
                        saved_intent, evidence, row.accounted_units, records,
                        confirmed_entry_commission=confirmed_entry_commission)
                delta = cumulative_units - row.accounted_units
                if delta < 0:
                    raise ValueError('Settlement cannot regress')
                terminal = (row.broker_status in ('CANCELLED', 'REJECTED')
                            or row.broker_status == 'FILLED' and cumulative_units == row.requested_units)
                if delta:
                    if row.unresolved_figi != intent.figi:
                        raise ValueError('Released intent cannot receive another fill')
                    self._validate_delta_trade(intent, trade, delta)
                    if row.position_snapshot is not None:
                        snapshot = CloseIntent.from_record(row).position_snapshot
                        if snapshot.entry_time is None:
                            raise ValueError('Confirmed entry time unavailable; keep execution pending accounting')
                        if (Decimal(str(trade.entry_price)).quantize(Decimal('0.000001')) != snapshot.entry_price.quantize(Decimal('0.000001'))
                                or trade.ticker != snapshot.ticker or trade.strategy_id != snapshot.strategy_id
                                or trade.exit_reason != snapshot.exit_reason
                                or snapshot.internal_account_id is not None and trade.account_id != snapshot.internal_account_id
                                or snapshot.entry_time is not None and trade.entry_time != snapshot.entry_time):
                            raise ValueError('Delta trade differs from saved original position')
                    db.add(trade)
                    await db.flush()
                    trade_id = trade.id
                    db.add(BrokerOrderFill(mode=intent.mode, broker_account_id=intent.broker_account_id,
                                           client_order_id=intent.client_order_id,
                                           cumulative_units=cumulative_units, executed_units=delta,
                                           trade_id=trade_id, execution_evidence=execution_evidence))
                    row.accounted_units = cumulative_units
                else:
                    fill = await db.scalar(select(BrokerOrderFill).where(
                        BrokerOrderFill.mode == intent.mode,
                        BrokerOrderFill.broker_account_id == intent.broker_account_id,
                        BrokerOrderFill.client_order_id == intent.client_order_id,
                        BrokerOrderFill.cumulative_units == cumulative_units,
                    ))
                    if fill is None:
                        raise ValueError('Accounted quantity has no durable fill record')
                    trade_id = fill.trade_id
                if terminal and (row.runtime_accounted_units is None
                                 or row.runtime_accounted_units == cumulative_units):
                    row.unresolved_figi = None
                    row.status = 'SETTLED'
                elif row.runtime_accounted_units is not None and row.runtime_accounted_units < cumulative_units:
                    row.status = 'ACCOUNTED_PENDING_RUNTIME'
                result = CloseSettlement(delta, cumulative_units, intent.requested_units - cumulative_units,
                                         row.unresolved_figi is None, trade_id)
            return result

    async def apply_runtime(self, intent: CloseIntent, *, cumulative_units: int, apply) -> CloseSettlement:
        """Commit local effects and acknowledgement together; apply must use this session without committing."""
        intent.validate()
        if intent.action != "close":
            raise ValueError("Opening intent requires entry accounting")
        if type(cumulative_units) is not int or cumulative_units <= 0 or not callable(apply):
            raise ValueError('Explicit cumulative runtime work and database handler required')
        async with self.sessions() as db:
            async with db.begin():
                row = await db.scalar(select(BrokerOrderIntent).where(
                    BrokerOrderIntent.action == intent.action,
                    BrokerOrderIntent.mode == intent.mode,
                    BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                    BrokerOrderIntent.client_order_id == intent.client_order_id,
                ).with_for_update())
                if (row is None or row.figi != intent.figi or row.side != intent.side
                        or row.requested_units != intent.requested_units or row.lot_size != intent.lot_size
                        or row.runtime_accounted_units is None or row.accounted_units != cumulative_units):
                    raise ValueError('Runtime work differs from durable settlement')
                delta = cumulative_units - row.runtime_accounted_units
                trade_id = None
                if delta:
                    records = (await db.execute(select(BrokerOrderFill, PaperTrade).join(
                        PaperTrade, PaperTrade.id == BrokerOrderFill.trade_id).where(
                        BrokerOrderFill.mode == intent.mode,
                        BrokerOrderFill.broker_account_id == intent.broker_account_id,
                        BrokerOrderFill.client_order_id == intent.client_order_id,
                        BrokerOrderFill.cumulative_units > row.runtime_accounted_units,
                        BrokerOrderFill.cumulative_units <= cumulative_units,
                    ).order_by(BrokerOrderFill.cumulative_units))).all()
                    covered = row.runtime_accounted_units
                    for fill, trade in records:
                        covered += fill.executed_units
                        if (fill.cumulative_units != covered or trade.qty != fill.executed_units
                                or trade.figi != intent.figi):
                            raise ValueError('Runtime work has an incomplete fill chain')
                    if covered != cumulative_units:
                        raise ValueError('Runtime work has missing durable trades')
                    await apply(db, CloseIntent.from_record(row), tuple(trade for _, trade in records))
                    row.runtime_accounted_units = cumulative_units
                    trade_id = records[-1][1].id
                terminal = (row.broker_status in ('CANCELLED', 'REJECTED')
                            or row.broker_status == 'FILLED' and cumulative_units == row.requested_units)
                if terminal and row.observed_units == cumulative_units:
                    row.unresolved_figi = None
                    row.status = 'SETTLED'
                result = CloseSettlement(delta, cumulative_units, intent.requested_units - cumulative_units,
                                         row.unresolved_figi is None, trade_id)
            return result

    @staticmethod
    def _validate_delta_trade(intent, trade, delta):
        from sqlalchemy import inspect
        if (not isinstance(trade, PaperTrade) or not inspect(trade).transient or trade.id is not None
                or trade.figi != intent.figi or trade.side != ('LONG' if intent.side == 'SELL' else 'SHORT')
                or type(trade.qty) is not int or trade.qty != delta):
            raise ValueError('A new trade for the exact unaccounted delta is required')
        for name in ('entry_price', 'exit_price', 'gross_pnl', 'commission', 'slippage', 'net_pnl'):
            value = getattr(trade, name, None)
            if isinstance(value, bool) or value is None:
                raise ValueError('Finite trade accounting required')
            try:
                number = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError('Finite trade accounting required') from error
            if not number.is_finite() or name in ('entry_price', 'exit_price') and number <= 0:
                raise ValueError('Finite positive execution prices required')
        if any(not isinstance(getattr(trade, name, None), datetime)
               or getattr(trade, name).utcoffset() is None for name in ('entry_time', 'exit_time')):
            raise ValueError('Aware execution times required')
