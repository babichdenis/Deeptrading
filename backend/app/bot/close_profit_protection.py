from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class CloseProfitProtection:
    activation_atr: float = 2.0
    distance_atr: float = 1.0
    commission_rate: float = 0.0005
    slippage_fraction: float = 0.0002

    def __post_init__(self):
        if not all(isfinite(x) for x in (self.activation_atr, self.distance_atr, self.commission_rate, self.slippage_fraction)):
            raise ValueError('Finite protection parameters required')
        if not 0 < self.distance_atr < self.activation_atr:
            raise ValueError('Require 0 < distance < activation')
        if not 0 <= self.commission_rate < 1 or not 0 <= self.slippage_fraction < 1:
            raise ValueError('Invalid costs')

    def stop_after_close(self, position, bar):
        if position.atr <= 0 or not isfinite(position.atr):
            raise ValueError('Positive entry ATR required')
        breakeven = (position.entry_fill + position.entry_commission / position.qty) / ((1 - self.slippage_fraction) * (1 - self.commission_rate))
        if bar.close < position.entry_raw + self.activation_atr * position.atr:
            return position.stop
        candidate = max(breakeven, bar.close - self.distance_atr * position.atr)
        return max(position.stop, candidate) if candidate < bar.close else position.stop
