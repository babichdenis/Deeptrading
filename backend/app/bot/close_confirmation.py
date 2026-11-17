from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class CloseConfirmation:
    status: str
    reason: str | None
    executed_units: int | None = None
    remaining_units: int | None = None
    exit_price: float | None = None

    @property
    def fully_closed(self):
        return self.status == 'FILLED'


def classify_close_confirmation(trade, *, requested_units, client_order_id=None):
    """Classify one order's cumulative confirmed fill; never infer broker-flat or permit retries."""
    if type(requested_units) is not int or requested_units <= 0:
        raise ValueError('Positive integer requested shares required')
    if client_order_id is not None and (not isinstance(client_order_id, str) or not client_order_id.strip()):
        raise ValueError('Nonempty client order identity required')
    if trade is None:
        return CloseConfirmation('PENDING_RECONCILIATION', 'unknown_broker_result')
    if client_order_id is not None and getattr(trade, 'client_order_id', None) != client_order_id:
        return CloseConfirmation('PENDING_RECONCILIATION', 'execution_identity_unknown')
    executed = getattr(trade, 'qty', None)
    if type(executed) is not int or not 0 < executed <= requested_units:
        return CloseConfirmation('PENDING_RECONCILIATION', 'execution_quantity_unknown')
    raw_price = getattr(trade, 'exit_price', None)
    try:
        price = float(raw_price)
    except (TypeError, ValueError, OverflowError):
        price = None
    if isinstance(raw_price, bool) or price is None or not isfinite(price) or price <= 0:
        return CloseConfirmation('PENDING_RECONCILIATION', 'execution_price_unknown',
                                 executed, requested_units - executed)
    remaining = requested_units - executed
    return CloseConfirmation('PARTIAL_FILLED' if remaining else 'FILLED',
                             'partial_execution' if remaining else None,
                             executed, remaining, price)
