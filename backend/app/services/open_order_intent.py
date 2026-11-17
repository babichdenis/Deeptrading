from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from app.services.broker_order_journal import OrderIntent
from app.services.exit_policy_snapshot import ExitPolicySnapshot


@dataclass(frozen=True)
class OpenIntent(OrderIntent):
    action: ClassVar[str] = 'open'
    ticker: str = ''
    strategy_id: str = ''
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    exit_policy: ExitPolicySnapshot | None = None

    def validate(self):
        super().validate()
        if self.exit_policy is not None:
            if not isinstance(self.exit_policy, ExitPolicySnapshot):
                raise ValueError("Typed exit policy required")
            self.exit_policy.restore()
        if not isinstance(self.ticker, str) or not self.ticker.strip() or len(self.ticker) > 32:
            raise ValueError('Opening ticker required')
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip() or len(self.strategy_id) > 64:
            raise ValueError('Opening strategy required')
        for value in (self.stop_loss, self.take_profit):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value <= 0):
                raise ValueError('Finite positive decimal protection level required')
        if self.stop_loss is not None and self.take_profit is not None:
            if ((self.side == 'BUY' and self.stop_loss >= self.take_profit)
                    or (self.side == 'SELL' and self.stop_loss <= self.take_profit)):
                raise ValueError('Protection levels conflict with entry direction')

    def entry_payload(self):
        self.validate()
        return dict(version=2, exit_policy=self.exit_policy.to_payload() if self.exit_policy else None, ticker=self.ticker, strategy_id=self.strategy_id,
                    stop_loss=str(self.stop_loss) if self.stop_loss is not None else None,
                    take_profit=str(self.take_profit) if self.take_profit is not None else None)

    @classmethod
    def from_record(cls, row):
        payload = row.entry_plan
        if row.action != 'open' or row.position_snapshot is not None or not isinstance(payload, dict):
            raise ValueError('Typed opening intent required')
        if type(payload.get('version')) is not int or payload['version'] not in (1, 2):
            raise ValueError('Unsupported opening plan version')
        expected = {'version', 'ticker', 'strategy_id', 'stop_loss', 'take_profit'}
        if payload['version'] == 2:
            expected.add('exit_policy')
        if set(payload) != expected:
            raise ValueError('Incomplete or unknown opening plan fields')
        try:
            levels = [Decimal(payload[key]) if isinstance(payload[key], str) else None
                      for key in ('stop_loss', 'take_profit')]
            if any(payload[key] is not None and not isinstance(payload[key], str)
                   for key in ('stop_loss', 'take_profit')):
                raise ValueError('Decimal string required')
        except InvalidOperation as error:
            raise ValueError('Invalid opening protection level') from error
        result = cls(mode=row.mode, broker_account_id=row.broker_account_id,
                     client_order_id=row.client_order_id, figi=row.figi, side=row.side,
                     requested_units=row.requested_units, lot_size=row.lot_size, created_at=row.created_at,
                     ticker=payload['ticker'], strategy_id=payload['strategy_id'],
                     stop_loss=levels[0], take_profit=levels[1],
                     exit_policy=ExitPolicySnapshot.from_payload(payload["exit_policy"]) if payload.get("exit_policy") is not None else None)
        result.validate()
        return result
