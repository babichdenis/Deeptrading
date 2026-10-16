"""B2 EXECUTION CONTRACT: single shared core for A7 run_day and A24 execute.

Pure stdlib, no app.* imports, no I/O, no network, no DB. Both shells
(day_strategies_core.run_day and day_candidate_execution.execute_candidate_signal)
call walk_exit/settle_entry/settle_exit/none_incomplete; parallel copies of these
rules are forbidden. Formulas keep the exact float operation order of the shells
so outputs stay byte-identical after delegation.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
TIME_EXIT_MOD = 1110
PLAN_WINDOW_MOD = 1100


def msk_mod(ts: datetime) -> int:
    loc = ts.astimezone(MSK)
    return loc.hour * 60 + loc.minute


def gap_reason_for(mod: int) -> str:
    if mod == TIME_EXIT_MOD:
        return "time_exit_minute_18_30_missing"
    if PLAN_WINDOW_MOD <= mod < TIME_EXIT_MOD:
        return "time_exit_bar_18_20_incomplete"
    return "path_gap_after_entry"


def walk_exit(day_minutes, minute_by_ts, *, day, entry_ts, side, sl, tp,
              full_18_20: bool, has_18_30: bool):
    """Position path over session minutes of day from entry (inclusive).

    Returns ("exit", exit_base, exit_ts, reason), ("incomplete", reason, gap_ts)
    or None (no exit to end of data; resolved by none_incomplete). Continuity is
    mandatory: a stop executed before the first gap stays FILLED; anything
    reaching a gap stays INCOMPLETE and later candles cannot cure it. The
    planned exit (close of full 18:20 -> open of 18:30) has priority over SL/TP
    on that minute; SL is checked before TP inside a bar (pessimistic).
    """
    plan_ok = full_18_20 and has_18_30
    for m in day_minutes:
        if m.ts < entry_ts:
            continue
        prev_ts = m.ts - timedelta(minutes=1)
        if prev_ts >= entry_ts and prev_ts not in minute_by_ts:
            return ("incomplete", gap_reason_for(msk_mod(prev_ts)), prev_ts)
        if msk_mod(m.ts) == TIME_EXIT_MOD and full_18_20:
            return ("exit", m.open, m.ts, "time_exit_18_30")
        if msk_mod(m.ts) > TIME_EXIT_MOD and not plan_ok:
            plan_reason = ("time_exit_bar_18_20_incomplete" if not full_18_20
                           else "time_exit_minute_18_30_missing")
            return ("incomplete", plan_reason, m.ts)
        if side > 0 and m.open <= sl:
            return ("exit", m.open, m.ts, "sl_gap_open")
        if side < 0 and m.open >= sl:
            return ("exit", m.open, m.ts, "sl_gap_open")
        if tp is not None:
            if side > 0 and m.open >= tp:
                return ("exit", tp, m.ts, "tp_gap_open")
            if side < 0 and m.open <= tp:
                return ("exit", tp, m.ts, "tp_gap_open")
        if side > 0 and m.low <= sl:
            return ("exit", sl, m.ts, "sl_touch")
        if side < 0 and m.high >= sl:
            return ("exit", sl, m.ts, "sl_touch")
        if tp is not None:
            if side > 0 and m.high >= tp:
                return ("exit", tp, m.ts, "tp_touch")
            if side < 0 and m.low <= tp:
                return ("exit", tp, m.ts, "tp_touch")
    return None


def none_incomplete(day_minutes, *, full_18_20: bool, has_18_30: bool) -> str:
    """Incomplete policy for walk_exit None: never close at the last close;
    distinguish the uncovered planned exit from a truncated day."""
    entered_plan_window = any(msk_mod(b.ts) >= PLAN_WINDOW_MOD
                              for b in day_minutes)
    if entered_plan_window and not full_18_20:
        return "time_exit_bar_18_20_incomplete"
    if entered_plan_window and not has_18_30:
        return "time_exit_minute_18_30_missing"
    return "data_ended_open_position"


def settle_entry(entry_base: float, side: int, costs) -> dict:
    """Entry fills with adverse slippage; costs duck-typed (.fill/.commission)."""
    entry_fill = costs.fill(entry_base, side)
    slip_entry = entry_fill - entry_base
    commission_entry = costs.commission(entry_fill)
    return {"entry_fill": entry_fill, "slip_entry": slip_entry,
            "commission_entry": commission_entry}


def settle_exit(entry_base: float, exit_base: float, side: int, costs, *,
                entry_fill: float, commission_entry: float) -> dict:
    """Exit fills and accounting; net == gross - costs_total by construction.

    Returns exactly the stored output fields (no intermediate fill_delta)."""
    exit_fill = costs.fill(exit_base, -side)
    slip_exit = exit_fill - exit_base
    commission_exit = costs.commission(exit_fill)
    gross = side * (exit_base - entry_base)
    fill_delta = side * (exit_fill - entry_fill)
    net = fill_delta - commission_entry - commission_exit
    costs_total = commission_entry + commission_exit + (gross - fill_delta)
    if not all(math.isfinite(v) for v in (gross, fill_delta, net, costs_total)):
        raise ValueError("nonfinite_trade_result")
    return {"exit_fill": exit_fill, "slip_exit": slip_exit,
            "commission_exit": commission_exit, "gross": gross,
            "net": net, "costs_total": costs_total}
