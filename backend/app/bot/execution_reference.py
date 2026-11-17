from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


def require_aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('execution_reference_requires_aware_time')


@dataclass(frozen=True)
class ExecutionReference:
    figi: str
    price: float
    market_at: datetime
    received_at: datetime

    def validate(self, *, figi: str, decision_at: datetime, as_of: datetime,
                 max_age: timedelta = timedelta(seconds=30)):
        for value in (self.market_at, self.received_at, decision_at, as_of):
            require_aware(value)
        if not figi or self.figi != figi:
            raise ValueError('execution_reference_instrument_mismatch')
        if not isfinite(self.price) or self.price <= 0:
            raise ValueError('execution_reference_invalid_price')
        if max_age <= timedelta(0):
            raise ValueError('execution_reference_invalid_max_age')
        if not decision_at <= self.market_at <= self.received_at <= as_of:
            raise ValueError('execution_reference_time_order')
        if as_of - self.market_at > max_age:
            raise ValueError('execution_reference_stale')
        return self
