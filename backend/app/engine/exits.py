from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from app.engine.indicators import atr
from app.engine.models import Candle, ExitPlan, ExitReason, PositionState, Side


SAME_BAR_CONFLICT_RULE = "STOP_LOSS_FIRST"


class ExitPolicy(ABC):
    policy_id: str
    version: str

    @abstractmethod
    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan: ...


@dataclass(frozen=True)
class AtrTrailingPolicy(ExitPolicy):
    period: int = 14
    initial_stop_atr: float = 2.0
    activation_atr: float = 1.0
    trail_distance_atr: float = 2.0
    policy_id: str = "atr_trailing"
    version: str = "1.0.0"

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        values = atr(bars, self.period)
        last = values[-1] if values else None
        distance = (last or entry_price * 0.01) * self.initial_stop_atr
        if side is Side.BUY:
            return ExitPlan(stop_loss=entry_price - distance, take_profit=None)
        return ExitPlan(stop_loss=entry_price + distance, take_profit=None)

    def update_stop(
        self,
        side: Side,
        entry_price: float,
        current_stop: float | None,
        bars: Sequence[Candle],
    ) -> float | None:
        if len(bars) < 3:
            return current_stop
        window = bars[-self.period :]
        values = atr(bars, self.period)
        cur_atr = values[-1] if values else None
        if not cur_atr:
            return current_stop
        if side is Side.BUY:
            highest = max(b.high for b in window)
            move = highest - entry_price
            if move < self.activation_atr * cur_atr:
                return current_stop
            candidate = highest - self.trail_distance_atr * cur_atr
            return max(current_stop or candidate, candidate)
        lowest = min(b.low for b in window)
        move = entry_price - lowest
        if move < self.activation_atr * cur_atr:
            return current_stop
        candidate = lowest + self.trail_distance_atr * cur_atr
        stop = current_stop if current_stop is not None else candidate
        return min(stop, candidate)



@dataclass(frozen=True)
class FixedSlTpPolicy(ExitPolicy):
    stop_pct: float = 0.005
    target_pct: float = 0.01
    policy_id: str = "fixed_sl_tp"
    version: str = "1.0.0"

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        if side is Side.BUY:
            return ExitPlan(
                stop_loss=entry_price * (1 - self.stop_pct),
                take_profit=entry_price * (1 + self.target_pct),
            )
        return ExitPlan(
            stop_loss=entry_price * (1 + self.stop_pct),
            take_profit=entry_price * (1 - self.target_pct),
        )


@dataclass(frozen=True)
class AtrStopPolicy(ExitPolicy):
    period: int = 14
    multiplier: float = 2.0
    risk_reward: float | None = None
    trail_activation_r: float | None = None
    trail_distance_r: float | None = None
    policy_id: str = "atr_stop"
    version: str = "1.1.0"

    def _risk(self, entry_price: float, bars: Sequence[Candle]) -> float:
        values = atr(bars, self.period)
        last = values[-1] if values else None
        return (last or entry_price * 0.01) * self.multiplier

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        distance = self._risk(entry_price, bars)
        if side is Side.BUY:
            stop = entry_price - distance
            target = entry_price + distance * self.risk_reward if self.risk_reward else None
        else:
            stop = entry_price + distance
            target = entry_price - distance * self.risk_reward if self.risk_reward else None
        return ExitPlan(stop_loss=stop, take_profit=target)

    def update_stop(
        self,
        side: Side,
        entry_price: float,
        current_stop: float | None,
        bars: Sequence[Candle],
    ) -> float | None:
        """Трейлинг-стоп (активация на trail_activation_r × risk, дистанция
        trail_distance_r × risk). Вызывается движком, только если заданы
        trail_activation_r и trail_distance_r."""
        if self.trail_activation_r is None or self.trail_distance_r is None:
            return current_stop
        if len(bars) < 2:
            return current_stop
        risk = self._risk(entry_price, bars)
        if risk <= 0:
            return current_stop
        window = bars[-self.period :]
        if side is Side.BUY:
            highest = max(b.high for b in window)
            move = highest - entry_price
            if move < self.trail_activation_r * risk:
                return current_stop
            candidate = highest - self.trail_distance_r * risk
            return max(current_stop or candidate, candidate)
        lowest = min(b.low for b in window)
        move = entry_price - lowest
        if move < self.trail_activation_r * risk:
            return current_stop
        candidate = lowest + self.trail_distance_r * risk
        stop = current_stop if current_stop is not None else candidate
        return min(stop, candidate)


def intrabar_exit(
    bar: Candle,
    state: PositionState,
    stop_loss: float | None,
    take_profit: float | None,
) -> tuple[float | None, str]:
    if state is PositionState.LONG:
        if stop_loss is not None and bar.open <= stop_loss:
            return bar.open, ExitReason.STOP_LOSS.value
        if stop_loss is not None and bar.low <= stop_loss:
            return stop_loss, ExitReason.STOP_LOSS.value
        if take_profit is not None and bar.high >= take_profit:
            if bar.open >= take_profit:
                return bar.open, ExitReason.TARGET.value
            return take_profit, ExitReason.TARGET.value
        return None, ""
    if state is PositionState.SHORT:
        if stop_loss is not None and bar.open >= stop_loss:
            return bar.open, ExitReason.STOP_LOSS.value
        if stop_loss is not None and bar.high >= stop_loss:
            return stop_loss, ExitReason.STOP_LOSS.value
        if take_profit is not None and bar.low <= take_profit:
            if bar.open <= take_profit:
                return bar.open, ExitReason.TARGET.value
            return take_profit, ExitReason.TARGET.value
        return None, ""
    return None, ""
