from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from app.engine.models import Candle, DecisionAction, PositionState, Signal, Side


@dataclass(frozen=True)
class SignalPolicyConfig:
    id: str = "ignore_same_side"
    version: str = "1.0.0"
    min_hold_bars: int = 0
    allow_flip: bool = False
    same_side_reentry_cooldown_bars: int = 0
    exit_confirm_window_bars: int = 0
    entry_confirm_bars: int = 0  # N подряд подтверждающих свечей (close>open для LONG, close<open для SHORT) перед входом; 0 = без подтверждения
    opposite_hold: bool = False
    confirm_flip: bool = False
    # Лимитный вход: вместо market по open ставим лимит на k*ATR лучше цены сигнала.
    # 0 = выключено (market-вход, как раньше). Не исполнен за entry_limit_bars — отмена.
    entry_limit_atr: float = 0.0
    entry_limit_bars: int = 3
    entry_limit_chase: bool = False  # переносить лимит к цене каждый бар (chase)
    entry_limit_atr_period: int = 14


class SignalPolicy:
    def __init__(self, config: SignalPolicyConfig | None = None):
        self.config = config or SignalPolicyConfig()

    def decide(
        self,
        signal: Signal | None,
        state: PositionState,
        bars_held: int,
    ) -> tuple[DecisionAction, str]:
        if signal is None:
            return DecisionAction.NO_SIGNAL, ""
        if state is PositionState.FLAT:
            return DecisionAction.ACCEPT_ENTRY, f"entry {signal.reason}"
        same_side = (state is PositionState.LONG and signal.side is Side.BUY) or (
            state is PositionState.SHORT and signal.side is Side.SELL
        )
        if same_side:
            return (
                DecisionAction.IGNORE_SAME_SIDE,
                f"already {state.value}, {signal.side.value} ignored",
            )
        if self.config.min_hold_bars > 0 and bars_held < self.config.min_hold_bars:
            return (
                DecisionAction.REJECT_MIN_HOLD,
                f"bars_held={bars_held} < {self.config.min_hold_bars}",
            )
        action = "flip" if self.config.allow_flip else "exit"
        return DecisionAction.ACCEPT_EXIT, f"opposite {signal.reason} -> {action}"


class Strategy(Protocol):
    strategy_id: str
    version: str

    def warmup_bars(self) -> int: ...

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None: ...


@dataclass
class ScriptedStrategy:
    signals_by_index: dict[int, Signal] = field(default_factory=dict)
    strategy_id: str = "scripted"
    version: str = "1.0.0"
    warmup: int = 0

    def warmup_bars(self) -> int:
        return self.warmup

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        return self.signals_by_index.get(len(candles) - 1)
