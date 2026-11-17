from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def finite_decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('Invalid monetary value')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError('Invalid monetary value') from error
    if not result.is_finite() or positive and result <= 0:
        raise ValueError('Invalid monetary value')
    return result


@dataclass(frozen=True)
class ClosePositionSnapshot:
    ticker: str
    position_side: str
    position_units: int
    entry_price: Decimal
    observed_at: datetime
    entry_time: datetime | None = None
    entry_time_source: str = 'unknown'
    strategy_id: str = ''
    exit_reason: str = 'UNKNOWN'
    internal_account_id: int | None = None
    commission_rate: Decimal | None = None
    cost_basis: str = 'unknown'
    local_position_id: int | None = None
    entry_client_order_id: str | None = None

    def validate(self):
        if self.local_position_id is not None or self.entry_client_order_id is not None:
            if (type(self.local_position_id) is not int or self.local_position_id <= 0
                    or not isinstance(self.entry_client_order_id, str)
                    or not self.entry_client_order_id.strip() or len(self.entry_client_order_id) > 64):
                raise ValueError('Paired original local position and entry request identities required')
        if self.position_side not in ('LONG', 'SHORT') or type(self.position_units) is not int or self.position_units <= 0:
            raise ValueError('Invalid original position')
        finite_decimal(self.entry_price, positive=True)
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError('Aware snapshot time required')
        for value, maximum in ((self.ticker,32), (self.strategy_id,64), (self.exit_reason,32)):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError('Invalid position metadata')
        if self.entry_time_source not in ('unknown', 'execution_ledger', 'broker_history', 'sandbox_order_timestamp'):
            raise ValueError('Explicit entry time provenance required')
        if self.entry_time_source == 'unknown':
            if self.entry_time is not None:
                raise ValueError('Unknown entry time cannot be invented from snapshot time')
        elif (not isinstance(self.entry_time, datetime) or self.entry_time.utcoffset() is None
              or self.entry_time > self.observed_at):
            raise ValueError('Invalid confirmed entry time')
        if self.internal_account_id is not None and (type(self.internal_account_id) is not int or self.internal_account_id <= 0):
            raise ValueError('Invalid internal accounting identity')
        if self.cost_basis == 'unknown':
            if self.commission_rate is not None:
                raise ValueError('Unknown costs cannot carry an assumed rate')
        elif self.cost_basis == 'configured_estimate':
            rate = finite_decimal(self.commission_rate)
            if not 0 <= rate <= 1:
                raise ValueError('Invalid commission estimate')
        else:
            raise ValueError('Explicit commission provenance required')

    def validate_for(self, intent):
        self.validate()
        if self.entry_time_source == 'sandbox_order_timestamp' and intent.mode != 'sandbox':
            raise ValueError('Sandbox order timestamp forbidden outside sandbox')
        if (self.position_units != intent.requested_units
                or self.position_side != ('LONG' if intent.side == 'SELL' else 'SHORT')
                or self.observed_at > intent.created_at):
            raise ValueError('Snapshot does not match original close intent')

    def to_payload(self):
        self.validate()
        return dict(version=1, ticker=self.ticker, position_side=self.position_side,
                    position_units=self.position_units, entry_price=str(finite_decimal(self.entry_price)),
                    observed_at=self.observed_at.astimezone(timezone.utc).isoformat(),
                    entry_time=self.entry_time.astimezone(timezone.utc).isoformat() if self.entry_time else None,
                    entry_time_source=self.entry_time_source, strategy_id=self.strategy_id,
                    exit_reason=self.exit_reason, internal_account_id=self.internal_account_id,
                    commission_rate=str(finite_decimal(self.commission_rate)) if self.commission_rate is not None else None,
                    cost_basis=self.cost_basis, local_position_id=self.local_position_id,
                    entry_client_order_id=self.entry_client_order_id)

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or type(payload.get('version')) is not int or payload['version'] != 1:
            raise ValueError('Unknown position snapshot format')
        values = {key:value for key,value in payload.items() if key != 'version'}
        try:
            values['entry_price'] = finite_decimal(values['entry_price'], positive=True)
            values['observed_at'] = datetime.fromisoformat(values['observed_at'])
            values['entry_time'] = datetime.fromisoformat(values['entry_time']) if values.get('entry_time') else None
            values['commission_rate'] = finite_decimal(values['commission_rate']) if values.get('commission_rate') is not None else None
            result = cls(**values)
            result.validate()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Invalid stored position snapshot') from error
        return result
