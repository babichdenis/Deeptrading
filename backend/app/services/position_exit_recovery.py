import json
from dataclasses import dataclass
from math import isfinite

from app.services.close_position_projection import price_equal
from app.services.exit_policy_snapshot import ExitPolicySnapshot


@dataclass(frozen=True)
class RecoveredExit:
    policy: object
    side: str
    units: int
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    trailing_active: bool
    leverage: float


def recover_position_exit(row, inventory, *, mode, account):
    side = 'LONG' if row.side in ('BUY', 'LONG') else 'SHORT' if row.side in ('SELL', 'SHORT') else None
    if (row.mode != mode or row.broker_account_id != account or row.exit_time is not None
            or row.figi != inventory.figi or side is None or inventory.side != side
            or type(row.qty) is not int or row.qty <= 0 or type(inventory.qty) is not int
            or inventory.qty != row.qty or not price_equal(inventory.entry_price, row.entry_price)):
        raise ValueError('Saved position differs from scoped broker inventory')
    metadata = json.loads(row.meta or '{}')
    if not isinstance(metadata, dict):
        raise ValueError('Saved position metadata invalid')
    legacy = metadata.get('entry_exit_policy')
    plan = metadata.get('entry_plan')
    snapshot = plan.get('exit_policy') if isinstance(plan, dict) else None
    if legacy is not None and snapshot is not None and legacy != snapshot:
        raise ValueError('Saved exit policies conflict')
    payload = snapshot if snapshot is not None else legacy
    if payload is None:
        raise ValueError('Original exit policy unavailable')
    policy = ExitPolicySnapshot.from_payload(payload).restore()
    for level in (row.stop_loss, row.take_profit, row.entry_price):
        if level is not None and (not isfinite(float(level)) or float(level) <= 0):
            raise ValueError('Saved protection level invalid')
    if not isfinite(float(row.leverage)) or float(row.leverage) < 1:
        raise ValueError('Saved position leverage invalid')
    return RecoveredExit(policy, side, row.qty, float(row.entry_price),
        float(row.stop_loss) if row.stop_loss is not None else None,
        float(row.take_profit) if row.take_profit is not None and not row.trailing_active else None,
        bool(row.trailing_active), float(row.leverage))
