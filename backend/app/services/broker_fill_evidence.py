from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class ConfirmedFill:
    trade_id: str
    units: int
    price: Decimal
    executed_at: datetime


@dataclass(frozen=True)
class OrderFillEvidence:
    mode: str
    broker_account_id: str
    client_order_id: str
    broker_order_id: str
    figi: str
    side: str
    currency: str
    requested_lots: int
    executed_lots: int
    lot_size: int
    fills: tuple[ConfirmedFill, ...]

    @property
    def units(self):
        return sum(fill.units for fill in self.fills)

    @property
    def notional(self):
        return sum((fill.price * fill.units for fill in self.fills), Decimal(0))

    @property
    def average_price(self):
        return self.notional / self.units

    @property
    def first_execution_time(self):
        return min(fill.executed_at for fill in self.fills)

    @property
    def last_execution_time(self):
        return max(fill.executed_at for fill in self.fills)


def parse_order_state_fills(response, *, mode, broker_account_id, client_order_id,
                            figi, side, requested_lots, lot_size, as_of):
    """Parse GetOrderState stages; quantities are lots and prices are per instrument."""
    if (mode not in ('live', 'sandbox') or not isinstance(broker_account_id,str) or not broker_account_id.strip()
            or not isinstance(client_order_id,str) or not client_order_id.strip()
            or not isinstance(figi,str) or not figi.strip() or side not in ('BUY','SELL')
            or type(requested_lots) is not int or requested_lots <= 0
            or type(lot_size) is not int or lot_size <= 0
            or not isinstance(as_of,datetime) or as_of.utcoffset() is None):
        raise ValueError('Explicit scoped order and lot evidence required')
    from app.services.sandbox_order_evidence import SandboxOrderState, parse_sandbox_order_aggregate
    if isinstance(response, SandboxOrderState):
        return parse_sandbox_order_aggregate(response.raw, mode=mode,
            broker_account_id=broker_account_id, client_order_id=client_order_id,
            figi=figi, side=side, requested_lots=requested_lots, lot_size=lot_size, as_of=as_of)
    expected_direction = 1 if side == 'BUY' else 2
    broker_order_id = getattr(response,'order_id',None)
    direction = getattr(response,'direction',None)
    if (getattr(response,'order_request_id',None) != client_order_id
            or getattr(response,'figi',None) != figi or not isinstance(direction,int) or isinstance(direction,bool) or direction != expected_direction
            or type(getattr(response,'lots_requested',None)) is not int or response.lots_requested != requested_lots
            or not isinstance(broker_order_id,str) or not broker_order_id.strip()):
        raise ValueError('Broker order identity mismatch')
    executed = getattr(response,'lots_executed',None)
    status = getattr(response,'execution_report_status',None)
    if (type(executed) is not int or not 0 < executed <= requested_lots
            or not isinstance(status,int) or isinstance(status,bool) or status not in (1,2,3,4,5)
            or status in (2,4)
            or status == 1 and executed != requested_lots
            or status == 5 and executed >= requested_lots):
        raise ValueError('Broker cumulative fill evidence inconsistent')
    currency = getattr(response,'currency',None)
    if not isinstance(currency,str) or not currency.strip():
        raise ValueError('Execution currency unknown')
    order_date = getattr(response,'order_date',None)
    if (not isinstance(order_date,datetime) or order_date.utcoffset() is None
            or order_date <= datetime(1970,1,1,tzinfo=timezone.utc) or order_date > as_of):
        raise ValueError('Confirmed order placement time required')
    fills = []
    seen = set()
    for stage in getattr(response,'stages',None) or ():
        trade_id = getattr(stage,'trade_id',None)
        quantity = getattr(stage,'quantity',None)
        timestamp = getattr(stage,'execution_time',None)
        price = getattr(stage,'price',None)
        units = getattr(price,'units',None)
        nano = getattr(price,'nano',None)
        if (not isinstance(trade_id,str) or not trade_id.strip() or trade_id in seen
                or type(quantity) is not int or quantity <= 0
                or not isinstance(timestamp,datetime) or timestamp.utcoffset() is None or not order_date <= timestamp <= as_of
                or type(units) is not int or type(nano) is not int or not -999999999 <= nano <= 999999999
                or getattr(price,'currency',None) != currency):
            raise ValueError('Incomplete or conflicting execution stage')
        per_unit_price = Decimal(units) + Decimal(nano) / Decimal(1000000000)
        if per_unit_price <= 0:
            raise ValueError('Positive instrument execution price required')
        seen.add(trade_id)
        fills.append(ConfirmedFill(trade_id,quantity * lot_size,per_unit_price,timestamp.astimezone(timezone.utc)))
    if sum(fill.units for fill in fills) != executed * lot_size:
        raise ValueError('Execution stages do not cover confirmed cumulative quantity')
    return OrderFillEvidence(mode,broker_account_id,client_order_id,broker_order_id,figi,side,currency,
                             requested_lots,executed,lot_size,tuple(sorted(fills,key=lambda fill:(fill.executed_at,fill.trade_id))))


async def read_order_fill_evidence(broker, *, mode, broker_account_id, client_order_id,
                                   figi, side, requested_lots, lot_size, as_of):
    if (broker.mode,broker._account) != (mode,broker_account_id):
        raise ValueError('Broker scope changed before history lookup')
    response = await broker.order_state(client_order_id,figi,side,requested_lots)
    if (broker.mode,broker._account) != (mode,broker_account_id):
        raise ValueError('Broker scope changed during history lookup')
    return parse_order_state_fills(response,mode=mode,broker_account_id=broker_account_id,
                                  client_order_id=client_order_id,figi=figi,side=side,
                                  requested_lots=requested_lots,lot_size=lot_size,as_of=as_of)


def entry_evidence_metadata(evidence, *, mode, broker_account_id, client_order_id, figi,
                            side, units, lot_size, price):
    if evidence is None:
        return {'entry_time_source':'unknown', 'entry_time_confirmed':None, 'entry_fill_evidence':None}
    if (not isinstance(evidence,OrderFillEvidence)
            or (evidence.mode,evidence.broker_account_id,evidence.client_order_id,evidence.figi,evidence.side)
            != (mode,broker_account_id,client_order_id,figi,side)
            or evidence.units != units or evidence.lot_size != lot_size
            or evidence.average_price.quantize(Decimal('0.000001')) != Decimal(str(price)).quantize(Decimal('0.000001'))):
        raise ValueError('Entry evidence does not match the persisted execution')
    return {'entry_time_source':'broker_history',
            'entry_time_confirmed':evidence.first_execution_time.isoformat(),
            'entry_fill_evidence':dict(version=1,client_order_id=evidence.client_order_id,
                broker_order_id=evidence.broker_order_id,figi=evidence.figi,side=evidence.side,
                currency=evidence.currency,requested_lots=evidence.requested_lots,
                executed_lots=evidence.executed_lots,lot_size=evidence.lot_size,
                fills=[dict(trade_id=fill.trade_id,units=fill.units,price=str(fill.price),
                            executed_at=fill.executed_at.isoformat()) for fill in evidence.fills])}
