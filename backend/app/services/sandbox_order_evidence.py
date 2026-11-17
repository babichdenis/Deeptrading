from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def money(value, currency):
    units, nano = getattr(value, 'units', None), getattr(value, 'nano', None)
    if (type(units) is not int or type(nano) is not int or abs(nano) >= 1000000000
            or getattr(value, 'currency', None) != currency
            or units > 0 and nano < 0 or units < 0 and nano > 0):
        raise ValueError('sandbox_aggregate_money_invalid')
    return Decimal(units) + Decimal(nano) / Decimal(1000000000)


@dataclass(frozen=True)
class SandboxOrderAggregate:
    broker_account_id: str
    client_order_id: str
    broker_order_id: str
    figi: str
    side: str
    lot_size: int
    requested_lots: int
    units: int
    notional: Decimal
    commission: Decimal
    order_at: datetime
    observed_at: datetime
    currency: str
    source: str = 'sandbox_order_aggregate'

    @property
    def average_price(self):
        return self.notional / self.units


def parse_sandbox_order_aggregate(response, *, mode, broker_account_id,
                                   client_order_id, figi, side, requested_lots,
                                   lot_size, as_of):
    if mode != 'sandbox':
        raise ValueError('sandbox_aggregate_forbidden_in_live')
    if (not isinstance(broker_account_id, str) or not broker_account_id.strip()
            or not isinstance(client_order_id, str) or not client_order_id.strip()
            or not isinstance(figi, str) or not figi.strip()
            or side not in ('BUY', 'SELL') or type(lot_size) is not int or lot_size <= 0
            or type(requested_lots) is not int or requested_lots <= 0):
        raise ValueError('sandbox_aggregate_scope_invalid')
    if (response.order_request_id != client_order_id or response.figi != figi
            or type(response.lots_requested) is not int or response.lots_requested != requested_lots
            or response.direction != (1 if side == 'BUY' else 2)
            or not isinstance(response.order_id, str) or not response.order_id.strip()):
        raise ValueError('sandbox_aggregate_identity_mismatch')
    if (response.execution_report_status != 1 or response.stages
            or type(response.lots_executed) is not int
            or response.lots_executed != requested_lots * lot_size):
        raise ValueError('sandbox_aggregate_requires_complete_unit_response')
    order_at = response.order_date
    if (not isinstance(order_at, datetime) or order_at.utcoffset() is None
            or not isinstance(as_of, datetime) or as_of.utcoffset() is None
            or order_at.year < 2000 or order_at > as_of):
        raise ValueError('sandbox_aggregate_time_invalid')
    if response.currency != 'rub':
        raise ValueError('sandbox_aggregate_currency_unsupported')
    notional = money(response.total_order_amount, response.currency)
    reported_price = money(response.executed_order_price, response.currency)
    commission = money(response.executed_commission, response.currency)
    if notional <= 0 or reported_price != notional or commission < 0:
        raise ValueError('sandbox_aggregate_amount_inconsistent')
    return SandboxOrderAggregate(broker_account_id, client_order_id, response.order_id,
        figi, side, lot_size, requested_lots, response.lots_executed, notional,
        commission, order_at, as_of, response.currency)


def aggregate_metadata(intent, evidence, as_of):
    intent.validate()
    if (not isinstance(evidence, SandboxOrderAggregate) or intent.mode != 'sandbox'
            or (evidence.broker_account_id, evidence.client_order_id, evidence.figi, evidence.side)
            != (intent.broker_account_id, intent.client_order_id, intent.figi, intent.side)
            or evidence.lot_size != intent.lot_size or evidence.units != intent.requested_units
            or evidence.requested_lots * evidence.lot_size != evidence.units
            or evidence.currency != 'rub' or evidence.source != 'sandbox_order_aggregate' or not evidence.broker_order_id
            or not intent.created_at <= evidence.order_at <= evidence.observed_at <= as_of
            or not evidence.notional.is_finite() or evidence.notional <= 0
            or not evidence.commission.is_finite() or evidence.commission < 0):
        raise ValueError('sandbox_aggregate_does_not_match_opening')
    payload = dict(version=1, source=evidence.source, broker_order_id=evidence.broker_order_id,
        client_order_id=evidence.client_order_id, figi=evidence.figi, side=evidence.side,
        currency=evidence.currency, requested_lots=evidence.requested_lots,
        lot_size=evidence.lot_size, units=evidence.units, notional=str(evidence.notional),
        commission=str(evidence.commission), order_at=evidence.order_at.isoformat())
    return dict(entry_time_source='sandbox_order_timestamp', entry_time_confirmed=None,
                entry_fill_evidence=payload)


def accounting_totals(payload):
    if payload.get('source') == 'sandbox_order_aggregate':
        units = payload['units']
        if type(units) is not int or units <= 0:
            raise ValueError('sandbox_accounting_units_invalid')
        return units, Decimal(payload['notional']) / units, datetime.fromisoformat(payload['order_at'])
    fills = payload['fills']
    units = sum(fill['units'] for fill in fills)
    return (units, sum(Decimal(fill['price']) * fill['units'] for fill in fills) / units,
            min(datetime.fromisoformat(fill['executed_at']) for fill in fills))


@dataclass(frozen=True)
class SandboxOrderState:
    raw: object
    aggregate: SandboxOrderAggregate

    @property
    def lots_executed(self):
        return self.aggregate.units // self.aggregate.lot_size

    def __getattr__(self, name):
        return getattr(self.raw, name)
