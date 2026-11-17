from dataclasses import dataclass


@dataclass(frozen=True)
class OpenSubmission:
    mode: str
    broker_account_id: str
    client_order_id: str
    broker_order_id: str
    figi: str
    side: str
    requested_units: int
    observed_units: int
    lot_size: int
    broker_status: str
