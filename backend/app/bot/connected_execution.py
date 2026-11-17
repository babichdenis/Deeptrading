from dataclasses import replace
from datetime import timedelta

from app.engine.models import Side
from app.engine.protection import ProtectionState, evaluate_protection


def position_side(position):
    return Side(getattr(position, "side", Side.BUY))


def protection_trigger(position, bar):
    return evaluate_protection(
        bar, ProtectionState(position.stop, position.target), side=position_side(position),
        entry_price=position.entry_fill, entry_risk=None, bars_held=0,
        wick_tol=0.0, trailing_close_based=False,
    )


def execute_minute(portfolio, bars, at, on_open, close_policy=None):
    if at.utcoffset() is None:
        raise ValueError('Aware minute timestamp required')
    if any(bar.ts != at for bar in bars.values()):
        raise ValueError('Execution bar timestamp mismatch')
    for position in list(portfolio.positions.values()):
        bar = bars.get(position.figi)
        if bar is None:
            position.path_uncertainty = True
            if not hasattr(portfolio, 'path_uncertain_figis'):
                portfolio.path_uncertain_figis = set()
            portfolio.path_uncertain_figis.add(position.figi)
            portfolio.record(at, 'MISSING_BAR_UNCLASSIFIED', figi=position.figi)
            continue
        opening = replace(bar, high=bar.open, low=bar.open, close=bar.open)
        decision = protection_trigger(position, opening)
        if decision.exit_price is not None:
            portfolio.close_position(position, decision.exit_price, at, decision.exit_reason)
    on_open()
    close_at = at + timedelta(minutes=1)
    for position in list(portfolio.positions.values()):
        bar = bars.get(position.figi)
        if bar is None:
            continue
        decision = protection_trigger(position, bar)
        if decision.exit_price is not None:
            portfolio.close_position(position, decision.exit_price, close_at, decision.exit_reason)
    if close_policy is not None:
        for position in list(portfolio.positions.values()):
            bar = bars.get(position.figi)
            if bar is None:
                continue
            new_stop = close_policy.stop_after_close(position, bar)
            direction = 1 if position_side(position) is Side.BUY else -1
            if direction * (new_stop - position.stop) > 0:
                previous = position.stop
                position.stop = new_stop
                portfolio.record(close_at, 'STOP_UPDATED', figi=position.figi, previous=previous, stop=new_stop, available_at=close_at.isoformat())
