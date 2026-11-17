import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from app.services.broker_fill_evidence import OrderFillEvidence, entry_evidence_metadata
from app.services.open_order_intent import OpenIntent
from app.services.sandbox_order_evidence import SandboxOrderAggregate, aggregate_metadata, accounting_totals


@dataclass(frozen=True)
class OpenSettlement:
    applied_units: int
    accounted_units: int
    local_position_id: int


def opening_protection_matches(position, intent):
    if position.stop_loss == intent.stop_loss and position.take_profit == intent.take_profit:
        return True
    stop = position.stop_loss
    if (position.trailing_active is not True or not isinstance(stop, Decimal)
            or not stop.is_finite() or stop <= 0 or intent.stop_loss is None
            or position.take_profit is not None):
        return False
    return stop >= intent.stop_loss if intent.side == 'BUY' else stop <= intent.stop_loss


def validate_evidence(intent, evidence, as_of):
    intent.validate()
    if isinstance(evidence, SandboxOrderAggregate):
        return aggregate_metadata(intent, evidence, as_of)
    if (not isinstance(evidence, OrderFillEvidence) or not isinstance(as_of, datetime)
            or as_of.utcoffset() is None or not evidence.fills
            or type(evidence.requested_lots) is not int or type(evidence.lot_size) is not int
            or evidence.lot_size != intent.lot_size
            or not isinstance(evidence.currency, str)
            or not isinstance(evidence.broker_order_id, str) or not evidence.broker_order_id.strip()
            or evidence.requested_lots != intent.requested_units // intent.lot_size
            or type(evidence.executed_lots) is not int or evidence.executed_lots <= 0
            or evidence.executed_lots > evidence.requested_lots or evidence.currency.lower() != 'rub'):
        raise ValueError('Scoped RUB execution evidence required')
    seen = set()
    for fill in evidence.fills:
        if (not isinstance(fill.trade_id, str) or not fill.trade_id.strip() or fill.trade_id in seen
                or type(fill.units) is not int or fill.units <= 0 or fill.units % intent.lot_size
                or not isinstance(fill.price, Decimal) or not fill.price.is_finite() or fill.price <= 0
                or not isinstance(fill.executed_at, datetime) or fill.executed_at.utcoffset() is None
                or not intent.created_at <= fill.executed_at <= as_of):
            raise ValueError('Invalid or duplicate opening stage')
        seen.add(fill.trade_id)
    if evidence.units != evidence.executed_lots * intent.lot_size:
        raise ValueError('Opening stage quantity mismatch')
    return entry_evidence_metadata(evidence, mode=intent.mode, broker_account_id=intent.broker_account_id,
        client_order_id=intent.client_order_id, figi=intent.figi, side=intent.side,
        units=evidence.units, lot_size=intent.lot_size, price=evidence.average_price)


async def settle_open(sessions, intent: OpenIntent, evidence: OrderFillEvidence, *, as_of: datetime):
    metadata = validate_evidence(intent, evidence, as_of)
    metadata['client_order_id'] = intent.client_order_id
    metadata['lot_size'] = intent.lot_size
    metadata['entry_plan'] = intent.entry_payload()
    _, _, entry_time = accounting_totals(metadata['entry_fill_evidence'])
    async with sessions() as db:
        async with db.begin():
            row = await db.scalar(select(BrokerOrderIntent).where(
                BrokerOrderIntent.mode == intent.mode,
                BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                BrokerOrderIntent.client_order_id == intent.client_order_id,
            ).with_for_update())
            if row is None or OpenIntent.from_record(row) != intent:
                raise ValueError('Original opening reservation required')
            if (row.broker_order_id != evidence.broker_order_id or row.observed_units != evidence.units
                    or row.broker_status not in ('PARTIAL_FILLED', 'FILLED', 'CANCELLED')
                    or row.broker_status == 'FILLED' and evidence.units != intent.requested_units
                    or row.broker_status == 'PARTIAL_FILLED' and evidence.units >= intent.requested_units
                    or row.accounted_units > evidence.units):
                raise ValueError('Opening observation conflicts with accounting')
            previous = row.entry_accounting
            if previous is None:
                if row.accounted_units != 0 or row.unresolved_figi != intent.figi:
                    raise ValueError('Opening accounting checkpoint missing')
                existing = await db.scalar(select(SandboxTrade.id).where(
                    SandboxTrade.mode == intent.mode, SandboxTrade.broker_account_id == intent.broker_account_id,
                    SandboxTrade.figi == intent.figi, SandboxTrade.exit_time.is_(None)).limit(1))
                if existing is not None:
                    raise ValueError('Unlinked open position already exists')
                position = SandboxTrade(mode=intent.mode, broker_account_id=intent.broker_account_id,
                    figi=intent.figi, ticker=intent.ticker, side=intent.side, qty=evidence.units,
                    entry_time=entry_time, entry_price=evidence.average_price,
                    stop_loss=intent.stop_loss, take_profit=intent.take_profit,
                    entry_reason=intent.strategy_id, leverage=1.0, meta=json.dumps(metadata))
                db.add(position)
                await db.flush()
            else:
                if previous.get('version') != 1 or type(previous.get('local_position_id')) is not int:
                    raise ValueError('Invalid opening accounting checkpoint')
                old = previous['evidence']
                new = metadata['entry_fill_evidence']
                for key in ('broker_order_id', 'client_order_id', 'figi', 'side', 'currency', 'requested_lots', 'lot_size'):
                    if old[key] != new[key]:
                        raise ValueError('Opening evidence identity changed')
                if old.get('source') == 'sandbox_order_aggregate' or new.get('source') == 'sandbox_order_aggregate':
                    if old != new:
                        raise ValueError('Previously accounted sandbox aggregate changed')
                else:
                    stages = {fill['trade_id']: fill for fill in new['fills']}
                    if any(stages.get(fill['trade_id']) != fill for fill in old['fills']):
                        raise ValueError('Previously accounted opening stages changed')
                old_units, old_price, old_time = accounting_totals(old)
                if old_units != row.accounted_units:
                    raise ValueError('Previously accounted opening quantity changed')
                position = await db.get(SandboxTrade, previous['local_position_id'], with_for_update=True)
                position_meta = json.loads(position.meta or '{}') if position is not None else {}
                if (position is None or position.mode != intent.mode
                        or position.broker_account_id != intent.broker_account_id or position.figi != intent.figi
                        or position.side != intent.side or position.exit_time is not None
                        or position.qty != row.accounted_units
                        or position.entry_price != old_price.quantize(Decimal('0.000001'))
                        or position.entry_time != old_time
                        or not opening_protection_matches(position, intent)
                        or position_meta.get('entry_plan') != intent.entry_payload()
                        or position_meta.get('entry_fill_evidence') != old):
                    raise ValueError('Original local opening position changed')
                if evidence.units > row.accounted_units and row.unresolved_figi != intent.figi:
                    raise ValueError('Released opening cannot receive another fill')
                position.qty = evidence.units
                position.entry_price = evidence.average_price
                position.entry_time = entry_time
                position.meta = json.dumps({**position_meta, **metadata})
            delta = evidence.units - row.accounted_units
            row.accounted_units = evidence.units
            if row.runtime_accounted_units is None:
                row.runtime_accounted_units = 0
            row.entry_accounting = dict(version=1, local_position_id=position.id,
                                        evidence=metadata['entry_fill_evidence'])
            row.status = 'ACCOUNTED_PENDING_RUNTIME'
            await db.flush()
            result = OpenSettlement(delta, evidence.units, position.id)
        return result


@dataclass(frozen=True)
class OpenAcknowledgement:
    accounted_units: int
    released: bool


async def acknowledge_open(sessions, intent: OpenIntent, *, units: int, local_position_id: int):
    intent.validate()
    if type(units) is not int or units <= 0 or type(local_position_id) is not int or local_position_id <= 0:
        raise ValueError('Exact opening acknowledgement required')
    async with sessions() as db:
        async with db.begin():
            row = await db.scalar(select(BrokerOrderIntent).where(
                BrokerOrderIntent.mode == intent.mode, BrokerOrderIntent.broker_account_id == intent.broker_account_id,
                BrokerOrderIntent.client_order_id == intent.client_order_id).with_for_update())
            if (row is None or OpenIntent.from_record(row) != intent or row.accounted_units != units
                    or row.observed_units != units or row.entry_accounting is None
                    or row.entry_accounting.get('local_position_id') != local_position_id
                    or row.runtime_accounted_units is None):
                raise ValueError('Opening checkpoint changed before acknowledgement')
            position = await db.get(SandboxTrade, local_position_id, with_for_update=True)
            if (position is None or position.exit_time is not None or position.qty != units
                    or (position.mode, position.broker_account_id, position.figi, position.side)
                    != (intent.mode, intent.broker_account_id, intent.figi, intent.side)
                    or json.loads(position.meta or '{}').get('entry_fill_evidence') != row.entry_accounting['evidence']):
                raise ValueError('Opening local position changed before acknowledgement')
            evidence_units, price, first_time = accounting_totals(row.entry_accounting['evidence'])
            if (evidence_units != units
                    or position.entry_price != price.quantize(Decimal('0.000001'))
                    or position.entry_time != first_time
                    or json.loads(position.meta or '{}').get('entry_plan') != intent.entry_payload()):
                raise ValueError('Opening provenance changed before acknowledgement')
            terminal = row.broker_status == 'CANCELLED' or row.broker_status == 'FILLED' and units == intent.requested_units
            if row.broker_status not in ('PARTIAL_FILLED', 'FILLED', 'CANCELLED'):
                raise ValueError('Opening status is not confirmed')
            row.runtime_accounted_units = units
            row.status = 'SETTLED' if terminal else 'PENDING_RECONCILIATION'
            if terminal:
                row.unresolved_figi = None
            result = OpenAcknowledgement(units, terminal)
        return result
