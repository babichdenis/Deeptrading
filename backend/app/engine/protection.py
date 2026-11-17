from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.exits import ExitPolicy, breakeven_stop, early_abort_exit, intrabar_exit, partial_take_exit
from app.engine.models import Candle, PositionState, Side


@dataclass(frozen=True)
class ProtectionState:
    stop: float | None
    target: float | None
    trailing_active: bool = False


@dataclass(frozen=True)
class ProtectionUpdate:
    state: ProtectionState
    breakeven_changed: bool
    breakeven_stop: float | None
    trailing_activated: bool


def advance_protection(
    policy: ExitPolicy,
    state: ProtectionState,
    *,
    side: Side,
    entry_price: float,
    qty: int,
    entry_commission: float,
    closed_bars: Sequence[Candle],
    peak_price: float | None = None,
    entry_risk: float | None = None,
    be_trigger_r: float = 0.0,
    be_offset_pct: float = 0.0,
) -> ProtectionUpdate:
    """Compute next-bar protection after checking this bar against prior levels.

    Caller supplies only closed bars and persists the returned state. This
    transition neither checks fills nor sends orders; applying its levels to
    the supplied bar would introduce lookahead.
    """
    stop = state.stop
    if be_trigger_r > 0 and entry_risk is not None:
        stop = breakeven_stop(
            side, entry_price, stop, closed_bars,
            risk=entry_risk, trigger_r=be_trigger_r, offset_pct=be_offset_pct,
        )
    be_changed = stop != state.stop
    be_stop = stop
    update_stop = getattr(policy, "update_stop", None)
    if update_stop is not None:
        stop = update_stop(
            side, entry_price, stop, closed_bars,
            qty=qty, commission=entry_commission, peak_price=peak_price,
        )
    activated = False
    if not state.trailing_active:
        activate = getattr(policy, "trailing_activated", None)
        if activate is not None:
            activated = bool(activate(side, entry_price, qty, entry_commission, closed_bars))
    active = state.trailing_active or activated
    return ProtectionUpdate(
        state=ProtectionState(stop, None if active else state.target, active),
        breakeven_changed=be_changed,
        breakeven_stop=be_stop,
        trailing_activated=activated,
    )


@dataclass(frozen=True)
class ProtectionDecision:
    exit_price: float | None = None
    exit_reason: str = ""
    partial_price: float | None = None


def evaluate_protection(
    bar: Candle,
    state: ProtectionState,
    *,
    side: Side,
    entry_price: float,
    entry_risk: float | None,
    bars_held: int,
    wick_tol: float = 0.0,
    trailing_close_based: bool = False,
    abort_r: float = 0.0,
    abort_max_bars: int = 0,
    partial_r: float = 0.0,
    partial_eligible: bool = False,
) -> ProtectionDecision:
    """Evaluate prior levels; prices are triggers, not broker fill reports."""
    position = PositionState.LONG if side is Side.BUY else PositionState.SHORT
    price, reason = intrabar_exit(
        bar, position, state.stop, None if state.trailing_active else state.target,
        close_based=state.trailing_active and trailing_close_based,
        wick_tol=wick_tol,
    )
    if price is not None:
        return ProtectionDecision(price, reason)
    if abort_r > 0 and entry_risk is not None:
        price, reason = early_abort_exit(
            bar, side, entry_price, risk=entry_risk,
            bars_held=bars_held, max_bars=abort_max_bars, abort_r=abort_r,
        )
        if price is not None:
            return ProtectionDecision(price, reason)
    if partial_eligible and partial_r > 0 and entry_risk is not None:
        price = partial_take_exit(bar, side, entry_price, risk=entry_risk, partial_r=partial_r)
        if price is not None:
            return ProtectionDecision(partial_price=price)
    return ProtectionDecision()
