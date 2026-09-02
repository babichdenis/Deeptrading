"""AUDIT-ONLY слой для ensemble_main_v1 (read-only, движок не меняется).

Цель: сделать backtest воспроизводимым и объяснить каждый переход
candidate -> intent -> execution -> trade.

ИНВАРИАНТЫ:
- НЕ меняет signal logic, function set, quorum, cooldown, bias, entry/exit
  policy, stop/target, universe, sizing target, ML, session config.
- НЕ запускает parameter optimization.
- Все расчёты читают результат compute_ensemble (canonical pipeline) и
  ПЕРЕСЧИТЫВАЮТ только fills/P&L (deterministic reprice mode).

Содержит:
1. apply_fill_price — единая функция slippage (BUY/SELL × entry/exit).
2. reprice_trade — пересчёт fills/P&L существующей сделки (без изменения
   сигналов/выходов).
3. session_at — классификация по Europe/Moscow + MOEX main schedule.
4. funnel split по юнитам (setup/intent/trade) + reconciliation terminal states.
5. contention telemetry (same-timestamp intents).
6. intrabar exit policies (conservative_stop_first / optimistic_target_first /
   current_legacy).
7. exact lot sizing по fill price.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# MOEX main session: 10:00–18:45 MSK, Mon–Fri (пн-пт; праздники не моделируем)
MAIN_OPEN = (10, 0)
MAIN_CLOSE = (18, 45)


def apply_fill_price(side: str, action: str, raw_price: float, slippage_bps: float) -> float:
    """Единая функция fill accounting (audit).

    side:   LONG | SHORT (позиция)
    action: entry | exit
    BUY entry:  raw * (1 + s)     # покупаем дороже
    BUY exit:   raw * (1 - s)     # продаём дешевле
    SELL entry: raw * (1 - s)     # шортим дешевле
    SELL exit:  raw * (1 + s)     # откупаем дороже
    """
    s = slippage_bps / 10_000.0
    if side == "LONG":
        mult = 1 + s if action == "entry" else 1 - s
    else:  # SHORT
        mult = 1 - s if action == "entry" else 1 + s
    return raw_price * mult


def session_at(ts: datetime) -> str:
    """Классификация момента по MOEX main session (Europe/Moscow).

    Возвращает "main" | "outside" | "weekend".
    Не хардкодим UTC-окно: пересчитываем в MSK и проверяем 10:00–18:45 пн-пт.
    """
    lt = ts.astimezone(MSK)
    if lt.weekday() >= 5:
        return "weekend"
    t = (lt.hour, lt.minute)
    if MAIN_OPEN[0] * 60 + MAIN_OPEN[1] <= t[0] * 60 + t[1] <= MAIN_CLOSE[0] * 60 + MAIN_CLOSE[1]:
        return "main"
    return "outside"


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_msk(dt: datetime) -> datetime:
    return to_utc(dt).astimezone(MSK)


def qty_for_fill(capital_per_position: float, entry_fill_price: float, lot_size: int) -> int:
    """Точный sizing: qty = floor(capital / (entry_fill_price * lot)) * lot."""
    if entry_fill_price <= 0 or lot_size <= 0:
        return 0
    return int(capital_per_position // (entry_fill_price * lot_size)) * lot_size


# ---------------------------------------------------------------------------
# Intrabar exit policies
# ---------------------------------------------------------------------------

INTRABAR_POLICIES = ("conservative_stop_first", "optimistic_target_first", "current_legacy")


def detect_intrabar_ambiguity(bar: Any, state: str, stop: float | None, target: float | None) -> bool:
    """Оба барьера (stop и target) достижимы внутри одного бара по OHLC."""
    if stop is None or target is None:
        return False
    if state == "LONG":
        return bar.low <= stop and bar.high >= target
    return bar.high >= stop and bar.low <= target


def resolve_intrabar_exit(bar: Any, state: str, stop: float | None, target: float | None,
                          policy: str) -> tuple[float, str]:
    """Определяет цену/причину выхода при ambiguous баре по политике.

    current_legacy = как движок сейчас (intrabar_exit): stop проверяется
    раньше target; при обоих достижимых — стоп первым.
    conservative_stop_first: то же (стоп первым).
    optimistic_target_first: тейк первым.
    """
    ambiguous = detect_intrabar_ambiguity(bar, state, stop, target)
    if not ambiguous:
        # неоднозначности нет — обычная логика (как в движке)
        if state == "LONG":
            if stop is not None and bar.open <= stop:
                return bar.open, "stop_loss"
            if stop is not None and bar.low <= stop:
                return stop, "stop_loss"
            if target is not None and bar.high >= target:
                return bar.open if bar.open >= target else target, "target"
        else:
            if stop is not None and bar.open >= stop:
                return bar.open, "stop_loss"
            if stop is not None and bar.high >= stop:
                return stop, "stop_loss"
            if target is not None and bar.low <= target:
                return bar.open if bar.open <= target else target, "target"
        return None, ""
    # ambiguous: политика решает
    if policy == "optimistic_target_first":
        if state == "LONG":
            return bar.open if bar.open >= target else target, "target"
        return bar.open if bar.open <= target else target, "target"
    # conservative_stop_first / current_legacy → стоп первым
    if state == "LONG":
        if stop is not None and bar.open <= stop:
            return bar.open, "stop_loss"
        return stop, "stop_loss"
    if stop is not None and bar.open >= stop:
        return bar.open, "stop_loss"
    return stop, "stop_loss"


# ---------------------------------------------------------------------------
# Reprice existing trade (deterministic, fills/P&L only)
# ---------------------------------------------------------------------------

@dataclass
class RepricedTrade:
    trade_id: str
    figi: str
    side: str
    raw_entry_price: float
    entry_fill_price: float
    raw_exit_price: float
    exit_fill_price: float
    qty: int
    lot_size: int
    entry_notional: float
    exit_notional: float
    entry_slippage_rub: float
    exit_slippage_rub: float
    slippage_rub: float
    commission_rub: float
    total_cost_rub: float
    gross_rub: float
    net_rub: float
    break_even_bps: float
    exit_reason: str
    intrabar_ambiguous: bool
    intrabar_resolution_policy: str
    entry_time: datetime
    exit_time: datetime
    raw_bar: dict | None = None


def reprice_trade(
    t: dict,
    candles: list[Any],
    params: dict,
    intrabar_policy: str = "conservative_stop_first",
    entry_action: str = "entry",
) -> RepricedTrade:
    """Пересчёт fills/P&L существующей сделки.

    НЕ меняет сигналы/выходы: использует те же entry/exit бар, ту же причину
    выхода и тот же stop/target. Пересчитываются только fills, qty, costs, P&L
    с единой функцией apply_fill_price и slippage из params.
    """
    commission_rate = float(params.get("commission_rate", 0.0005))
    slippage_bps = float(params.get("slippage_bps", 2.0))
    capital = float(params.get("capital", 100_000))
    lot = int(params.get("lot", 10))

    side = "LONG" if str(t.get("side", "")).upper() in ("LONG", "BUY") else "SHORT"
    entry_idx = t.get("entry_index")
    exit_ts = t.get("exit_time") or t.get("exit_ts")
    exit_reason = t.get("exit_reason", "")
    stop = t.get("initial_stop") or t.get("stop")
    target = t.get("take_profit") or t.get("target")

    # raw entry = open бара входа (движок: entry по open следующего 1m бара)
    raw_entry = None
    if isinstance(entry_idx, int) and 0 <= entry_idx < len(candles):
        raw_entry = candles[entry_idx].open
    if raw_entry is None:
        raise ValueError(f"{t.get('trade_id')}: нет raw entry (entry_index={entry_idx})")

    entry_fill = apply_fill_price(side, "entry", raw_entry, slippage_bps)
    qty = qty_for_fill(capital, entry_fill, lot)
    if qty <= 0:
        qty = t.get("qty") or 1

    # raw exit: по причине выхода (барьер или бар)
    exit_idx = None
    raw_exit = None
    if exit_ts:
        target_dt = to_utc(datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00")))
        # бинарный поиск бара выхода
        lo, hi = 0, len(candles)
        while lo < hi:
            mid = (lo + hi) // 2
            if to_utc(candles[mid].ts) <= target_dt:
                lo = mid + 1
            else:
                hi = mid
        exit_idx = min(lo, len(candles) - 1)
        bar = candles[exit_idx]

        # сырая цена выхода по причине
        if exit_reason in ("target", "TARGET"):
            raw_exit = target if target is not None else bar.close
        elif exit_reason in ("stop_loss", "STOP_LOSS"):
            raw_exit = stop if stop is not None else bar.close
        else:
            raw_exit = bar.open  # signal/session/end → open бара выхода

        # intrabar ambiguity
        ambiguous = detect_intrabar_ambiguity(bar, side, stop, target)
        if ambiguous:
            raw_exit, exit_reason = resolve_intrabar_exit(bar, side, stop, target, intrabar_policy)
    else:
        exit_idx = len(candles) - 1
        bar = candles[exit_idx]
        raw_exit = bar.close
        ambiguous = False

    exit_fill = apply_fill_price(side, "exit", raw_exit, slippage_bps)
    entry_notional = entry_fill * qty
    exit_notional = exit_fill * qty
    entry_slip = abs(entry_fill - raw_entry) * qty
    exit_slip = abs(exit_fill - raw_exit) * qty
    slippage = entry_slip + exit_slip
    commission = commission_rate * (entry_notional + exit_notional)
    total_cost = commission + slippage
    gross = (exit_fill - entry_fill) * qty if side == "LONG" else (entry_fill - exit_fill) * qty
    net = gross - total_cost
    avg_notional = (entry_notional + exit_notional) / 2
    be_bps = total_cost / max(avg_notional, 1e-9) * 10000

    return RepricedTrade(
        trade_id=str(t.get("trade_id") or f"{t.get('figi')}:{entry_idx}"),
        figi=str(t.get("figi", "")),
        side=side,
        raw_entry_price=round(raw_entry, 6),
        entry_fill_price=round(entry_fill, 6),
        raw_exit_price=round(raw_exit, 6),
        exit_fill_price=round(exit_fill, 6),
        qty=qty,
        lot_size=lot,
        entry_notional=round(entry_notional, 2),
        exit_notional=round(exit_notional, 2),
        entry_slippage_rub=round(entry_slip, 2),
        exit_slippage_rub=round(exit_slip, 2),
        slippage_rub=round(slippage, 2),
        commission_rub=round(commission, 2),
        total_cost_rub=round(commission, 2) + round(slippage, 2),
        gross_rub=round(gross, 2),
        net_rub=round(net, 2),
        break_even_bps=round(be_bps, 2),
        exit_reason=exit_reason,
        intrabar_ambiguous=ambiguous,
        intrabar_resolution_policy=intrabar_policy,
        entry_time=to_utc(candles[entry_idx].ts) if isinstance(entry_idx, int) and 0 <= entry_idx < len(candles) else to_utc(datetime.fromisoformat(str(t.get("entry_time") or t.get("entry_ts")).replace("Z", "+00:00"))),
        exit_time=to_utc(datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))) if exit_ts else to_utc(candles[exit_idx].ts),
        raw_bar={"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "ts": bar.ts.isoformat()},
    )


# ---------------------------------------------------------------------------
# Session semantics
# ---------------------------------------------------------------------------

def intent_session_fields(decision_dt: datetime, execution_dt: datetime | None) -> dict:
    return {
        "decision_time_utc": to_utc(decision_dt).isoformat(),
        "decision_time_msk": to_msk(decision_dt).isoformat(),
        "execution_time_utc": to_utc(execution_dt).isoformat() if execution_dt else None,
        "execution_time_msk": to_msk(execution_dt).isoformat() if execution_dt else None,
        "session_at_decision": session_at(decision_dt),
        "session_at_execution": session_at(execution_dt) if execution_dt else None,
        "entry_session_policy": "main",
        "session_gate_result": "pass" if session_at(decision_dt) == "main" else "reject",
    }


# ---------------------------------------------------------------------------
# Funnel split by units + reconciliation
# ---------------------------------------------------------------------------

# Терминальные состояния intents
TERMINAL_STATES = (
    "EXECUTED",
    "REJECTED_SESSION",
    "REJECTED_COOLDOWN",
    "REJECTED_IN_POSITION",
    "REJECTED_DUPLICATE_EPISODE",
    "REJECTED_NO_NEXT_BAR",
    "REJECTED_PRICE_OR_LOT",
    "REJECTED_CAPITAL",
    "REJECTED_CONFLICT_PRIORITY",
    "REJECTED_OTHER",
)


def split_funnel(
    funnel: dict,
    rejected: list[dict],
    accepted: list[dict],
    trades: list[dict],
    reentry_rejected: list[dict],
) -> dict:
    """Разделяет funnel по юнитам: setup (function_signal/bar_event),
    intent (entry_intent), trade (trade). Никогда не считает pct_prev между
    разными юнитами.

    ВАЖНО (AUDIT находка): SessionPolicy.can_enter (sessions.py:48-49) режет
    ТОЛЬКО weekend (weekday>=5). Вне окна 10:00-18:45 MSK в будни — True
    (вход разрешён). Поэтому REJECTED_SESSION = weekend-решения; будни вне
    окна НЕ режутся движком и попадают в EXECUTED.
    """
    raw = funnel.get("raw_signals", 0)
    unique_raw = funnel.get("unique_raw_ts", 0)
    quorum = funnel.get("quorum_unique", 0)
    entries_raw = funnel.get("entries_raw", 0)
    intents = len(accepted)
    executed = len(trades)
    closed = len(trades)

    setup_funnel = [
        {"name": "raw_function_signals", "unit": "function_signal",
         "count": raw, "denominator_name": None, "denominator_count": None, "conversion_pct": None},
        {"name": "unique_raw_events", "unit": "bar_event",
         "count": unique_raw, "denominator_name": "raw_function_signals",
         "denominator_count": raw, "conversion_pct": round(unique_raw / max(raw, 1) * 100, 2)},
        {"name": "quorum_events", "unit": "bar_event",
         "count": quorum, "denominator_name": "unique_raw_events",
         "denominator_count": unique_raw, "conversion_pct": round(quorum / max(unique_raw, 1) * 100, 2)},
        {"name": "entry_candidates", "unit": "bar_event",
         "count": entries_raw, "denominator_name": "quorum_events",
         "denominator_count": quorum, "conversion_pct": round(entries_raw / max(quorum, 1) * 100, 2)},
    ]

    # intent funnel: терминальные состояния (только intents = accepted)
    # REJECTED_SESSION = решения вне main (движок после фикса sessions.py
    # режет любые входы вне окна 10:00-18:45 MSK + weekend)
    outside_n = 0
    for a in accepted:
        try:
            if session_at(to_utc(datetime.fromisoformat(str(a["ts"]).replace("Z", "+00:00")))) != "main":
                outside_n += 1
        except (ValueError, TypeError):
            continue
    terminal: dict[str, int] = {s: 0 for s in TERMINAL_STATES}
    terminal["EXECUTED"] = len(trades)
    terminal["REJECTED_SESSION"] = outside_n
    terminal["REJECTED_COOLDOWN"] = len(reentry_rejected)
    in_position = max(0, intents - len(trades) - outside_n - len(reentry_rejected))
    terminal["REJECTED_IN_POSITION"] = in_position

    intent_funnel = []
    for st in TERMINAL_STATES:
        intent_funnel.append({
            "name": st, "unit": "entry_intent", "count": terminal[st],
            "denominator_name": "entry_intents", "denominator_count": intents,
            "conversion_pct": round(terminal[st] / max(intents, 1) * 100, 2) if intents else None,
        })
    reconciled = sum(terminal.values())

    trade_funnel = [
        {"name": "executed_entries", "unit": "trade", "count": executed,
         "denominator_name": "entry_intents", "denominator_count": intents,
         "conversion_pct": round(executed / max(intents, 1) * 100, 2) if intents else None},
        {"name": "closed_trades", "unit": "trade", "count": closed,
         "denominator_name": "executed_entries", "denominator_count": executed,
         "conversion_pct": round(closed / max(executed, 1) * 100, 2) if executed else None},
    ]

    return {
        "setup_funnel": setup_funnel,
        "intent_funnel": intent_funnel,
        "trade_funnel": trade_funnel,
        "reconciliation": {
            "entry_intents": intents,
            "terminal_sum": reconciled,
            "reconciliation_pct": round(reconciled / max(intents, 1) * 100, 4) if intents else 100.0,
            "primary_terminal_reason": dict(terminal),
        },
    }


def contention_groups(accepted: list[dict]) -> list[dict]:
    """Группирует intents с одинаковым decision_time (контеншн).

    priority_rule: same-timestamp → LONG приоритетнее SHORT (детерминированно);
    при одинаковой стороне — первый в хронологическом порядке (стабильный).
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in accepted:
        groups[str(a.get("ts"))].append(a)
    out = []
    for ts, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        # приоритет: LONG > SHORT, затем порядок появления
        ranked = sorted(items, key=lambda x: (0 if x.get("side") == "BUY" else 1))
        for rank, it in enumerate(ranked):
            out.append({
                "conflict_group_id": f"cg:{ts}",
                "intent_id": f"{it.get('figi')}:{ts}:{it.get('side')}",
                "decision_time": ts,
                "side": it.get("side"),
                "candidates_in_group": len(items),
                "priority_rank": rank + 1,
                "priority_rule": "LONG>SHORT, then arrival order",
                "selected": rank == 0,
            })
    return out


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def trades_stats(rt: list[RepricedTrade]) -> dict:
    if not rt:
        return {"n": 0}
    nets = [t.net_rub for t in rt]
    gross_pos = sum(max(t.gross_rub, 0) for t in rt)
    gross_neg = sum(max(-t.gross_rub, 0) for t in rt)
    net_pos = sum(max(t.net_rub, 0) for t in rt)
    net_neg = sum(max(-t.net_rub, 0) for t in rt)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(rt, key=lambda x: x.entry_time):
        cum += t.net_rub
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    return {
        "n": len(rt),
        "gross": round(sum(t.gross_rub for t in rt), 2),
        "commission": round(sum(t.commission_rub for t in rt), 2),
        "slippage": round(sum(t.slippage_rub for t in rt), 2),
        "total_costs": round(sum(t.total_cost_rub for t in rt), 2),
        "net": round(sum(t.net_rub for t in rt), 2),
        "gross_pf": round(gross_pos / gross_neg, 2) if gross_neg > 0 else None,
        "net_pf": round(net_pos / net_neg, 2) if net_neg > 0 else None,
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "expectancy": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "median_net": round(sorted(nets)[len(nets) // 2], 2) if nets else 0.0,
        "max_drawdown_rub": round(max_dd, 2),
        "avg_break_even_bps": round(sum(t.break_even_bps for t in rt) / len(rt), 2) if rt else None,
        "intrabar_ambiguous_count": sum(1 for t in rt if t.intrabar_ambiguous),
        "avg_hold_minutes": round(sum((t.exit_time - t.entry_time).total_seconds() / 60 for t in rt) / len(rt), 1) if rt else None,
    }


# ---------------------------------------------------------------------------
# IN_POSITION decomposition (same-side / opposite / episode / awaiting exit)
# ---------------------------------------------------------------------------

IN_POSITION_REASONS = (
    "same_side_existing_position",
    "opposite_side_candidate",
    "same_episode_repeat",
    "new_episode_after_prior_entry",
    "position_awaiting_exit",
    "other_in_position",
)


def classify_in_position(intent_side: str, open_side: str | None, same_episode: bool) -> str:
    """Классификация отказа IN_POSITION.

    intent_side: BUY|SELL; open_side: LONG|SHORT|None.
    same_episode: intent относится к уже открытому эпизоду.
    """
    if open_side is None:
        return "position_awaiting_exit"
    same_side = (intent_side == "BUY" and open_side == "LONG") or (intent_side == "SELL" and open_side == "SHORT")
    if same_side:
        return "same_side_existing_position" if not same_episode else "same_episode_repeat"
    return "opposite_side_candidate"


def decompose_in_position(
    accepted: list[dict],
    trades: list[dict],
    reentry_rejected: list[dict],
    entries_raw_count: int,
) -> dict:
    """Разложение rejected-in-position по категориям.

    Оценка состояния позиции на момент intent: по хронологии сделок (entry/exit).
    same_episode: intent.ts попадает внутрь временного интервала открытой сделки.
    """
    # интервалы открытых позиций: (entry_ts, exit_ts, side)
    intervals = []
    for t in trades:
        e = t.get("entry_ts") or t.get("entry_time")
        x = t.get("exit_ts") or t.get("exit_time")
        if not e or not x:
            continue
        try:
            intervals.append((to_utc(datetime.fromisoformat(str(e).replace("Z", "+00:00"))),
                              to_utc(datetime.fromisoformat(str(x).replace("Z", "+00:00"))),
                              str(t.get("side", "")).upper()))
        except ValueError:
            continue

    def open_at(ts_dt: datetime) -> str | None:
        for e, x, side in intervals:
            if e <= ts_dt < x:
                return side
        return None

    counter: Counter = Counter()
    examples: dict[str, list[dict]] = defaultdict(list)
    executed_ts = {to_utc(datetime.fromisoformat(str((t.get("entry_ts") or t.get("entry_time"))).replace("Z", "+00:00")))
                   for t in trades if (t.get("entry_ts") or t.get("entry_time"))}
    cooldown_ts = {to_utc(datetime.fromisoformat(str(r.get("signal_ts")).replace("Z", "+00:00")))
                   for r in reentry_rejected if r.get("signal_ts")}

    for a in accepted:
        try:
            ts_dt = to_utc(datetime.fromisoformat(str(a["ts"]).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
        if ts_dt in executed_ts or ts_dt in cooldown_ts:
            continue  # исполнен или cooldown — не in-position
        open_side = open_at(ts_dt)
        if open_side is None:
            continue  # не in-position (другое терминальное состояние)
        same_ep = any(e <= ts_dt < x for e, x, _ in intervals)
        cat = classify_in_position(str(a["side"]), open_side, same_ep)
        counter[cat] += 1
        if len(examples[cat]) < 5:
            examples[cat].append({
                "intent_id": f"{a.get('figi')}:{a['ts']}:{a['side']}",
                "decision_time": a["ts"], "side": a["side"],
                "open_position_side": open_side,
            })

    return {
        "total_in_position": sum(counter.values()),
        "by_reason": {r: counter.get(r, 0) for r in IN_POSITION_REASONS},
        "share_of_intents_pct": round(sum(counter.values()) / max(len(accepted), 1) * 100, 2) if accepted else None,
        "examples": dict(examples),
        "interpretation": (
            "SignalPolicy.decide: same-side сигнал при открытой позиции -> IGNORE_SAME_SIDE "
            "(IN_POSITION); противоположный сигнал -> ACCEPT_EXIT (выход/flip), НЕ in-position."
        ),
    }


def session_invariants(trades: list[dict], accepted: list[dict]) -> dict:
    """Инварианты session policy (движок ИСПРАВЛЕН 2026-08-26):
    - can_enter режет ВСЕ входы вне окна 10:00-18:45 MSK + weekend;
    - new_entries_decision_outside_main должно быть 0 (все вне-main отсеяны);
    - fills_after_main_boundary = N (решение в main, исполнение позже границы)
    - exits_outside_main = N
    - overnight_positions = N
    """
    main_n = 0
    outside_n = 0
    weekend_n = 0
    for a in accepted:
        try:
            d = to_utc(datetime.fromisoformat(str(a["ts"]).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
        s = session_at(d)
        if s == "weekend":
            weekend_n += 1
        elif s == "outside":
            outside_n += 1
        else:
            main_n += 1
    fills_after = 0
    exits_outside = 0
    overnight = 0
    for t in trades:
        e = t.get("entry_ts") or t.get("entry_time")
        x = t.get("exit_ts") or t.get("exit_time")
        if not e or not x:
            continue
        try:
            ed = to_utc(datetime.fromisoformat(str(e).replace("Z", "+00:00")))
            xd = to_utc(datetime.fromisoformat(str(x).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
        if session_at(ed) == "main" and session_at(xd) != "main":
            fills_after += 1
        if session_at(xd) != "main":
            exits_outside += 1
        if ed.date() != xd.date():
            overnight += 1
    return {
        "new_entries_decision_main": main_n,
        "new_entries_decision_weekday_outside": outside_n,
        "new_entries_decision_weekend": weekend_n,
        "new_entries_decision_outside_main": outside_n + weekend_n,
        "fills_after_main_boundary": fills_after,
        "exits_outside_main": exits_outside,
        "overnight_positions": overnight,
        "policy": "движок исправлен: входы только в окне 10:00-18:45 MSK будни; вне — REJECTED_SESSION",
    }


# ---------------------------------------------------------------------------
# Intent lifecycle: точные terminal reasons через повторный прогон движка
# ---------------------------------------------------------------------------

INTENT_TERMINAL = (
    "EXECUTED_TRADE",
    "REJECTED_SESSION",
    "REJECTED_COOLDOWN",
    "REJECTED_IN_POSITION",
    "REJECTED_DUPLICATE_EPISODE",
    "REJECTED_CONFLICT_PRIORITY",
    "REJECTED_NO_NEXT_BAR",
    "REJECTED_LOT",
    "REJECTED_CAPITAL",
    "REJECTED_OTHER",
    "OPEN_AT_END",
    "CANCELLED_BEFORE_FILL",
    "LINKAGE_ERROR",
)


def replay_engine_audit(
    candles: list[Any],
    accepted: list[dict],
    entries_raw: list[dict],
    req: dict,
) -> tuple[list[dict], dict]:
    """Повторный прогон EngineRunner с теми же сигналами (движок НЕ меняется)
    и чтение ledger.audit — точные terminal reasons для каждого intent.

    Возвращает (intent_lifecycle, summary).
    intent_lifecycle: [{intent_id, decision_time, side, terminal, raw_reason,
                        reason_detail, linked_trade_id, episode_id, fill_ts,
                        reject_at, open_at_end}]
    """
    from app.engine.costs import CostModel
    from app.engine.policies import SignalPolicyConfig
    from app.engine.runner import EngineConfig, EngineRunner
    from app.engine.sessions import SessionPolicyConfig
    from app.engine.wave1 import ReplayStrategy
    from app.services.experiments import build_exit_policy

    exit_cfg = req.get("exit_policy", {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}})
    exit_obj = build_exit_policy(exit_cfg["id"], exit_cfg.get("params"))
    capital = float(req.get("capital", 10_000))
    lot = int(req.get("lot", 10))
    qty_shares = qty_for_fill(capital, candles[0].open if candles else 1.0, lot) or 1
    cfg_engine = EngineConfig(
        figi=str(req.get("figi", "")), qty=qty_shares, allow_short=True,
        cost_model=CostModel(commission_rate=float(req.get("commission_rate", 0.0005)),
                             slippage_bps=float(req.get("slippage_bps", 2.0))),
        signal_policy=SignalPolicyConfig(
            min_hold_bars=int(req.get("min_hold_bars", 0)),
            same_side_reentry_cooldown_bars=int(req.get("same_side_reentry_cooldown_bars", 0)),
            exit_confirm_window_bars=int(req.get("exit_confirm_window_bars", 0)),
            opposite_hold=bool(req.get("opposite_hold", False)),
            confirm_flip=bool(req.get("confirm_flip", False))),
        session_policy=SessionPolicyConfig(overnight=bool(req.get("carry_overnight", True))),
    )
    runner = EngineRunner(
        strategy=ReplayStrategy([(a["ts"], a["side"]) for a in accepted],
                                exits=[(e["ts"], e["side"]) for e in entries_raw]),
        exit_policy=exit_obj, config=cfg_engine)
    ledger = runner.run(candles)
    # trade_id -> entry_time для linkage
    trade_by_entry: dict[str, dict] = {}
    for t in ledger.trades:
        key = t.entry_time.isoformat() if hasattr(t.entry_time, "isoformat") else str(t.entry_time)
        trade_by_entry[key] = {"trade_id": t.trade_id, "side": t.side,
                               "net": t.net_pnl, "exit_reason": t.exit_reason,
                               "open_at_end": t.exit_reason in ("end_of_data", "END_OF_DATA", "session_close", "SESSION_CLOSE")}
    # аудит движка: точные raw-причины
    rej_map: dict[tuple[str, str], str] = {}
    for e in ledger.audit:
        if e.kind == "DECISION":
            d = e.detail
            ts_iso = e.time.isoformat()
            m_side = re.search(r"same-side (\w+)", d)
            side = m_side.group(1) if m_side else "?"
            if "REJECT_REENTRY" in d:
                rej_map[(ts_iso, side)] = "REJECTED_COOLDOWN"
            elif "REJECT_SESSION" in d:
                rej_map[(ts_iso, side)] = "REJECTED_SESSION"
            elif "SKIP_ENTRY" in d or "REJECT_SHORT" in d:
                rej_map[(ts_iso, side)] = "REJECTED_OTHER"
            elif "IGNORE_SAME_SIDE" in d:
                rej_map[(ts_iso, side)] = "REJECTED_IN_POSITION"

    life: list[dict] = []
    used_trades: set[str] = set()
    for a in accepted:
        ts = str(a["ts"])
        side = str(a["side"])
        intent_id = f"{req.get('figi')}:{ts}:{side}"
        try:
            ts_dt = to_utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except (ValueError, TypeError):
            ts_dt = None
        # 1) исполнена ли: ищем сделку с entry_time == ts или ближайший после (<=5 мин)
        linked = None
        if ts_dt is not None:
            candidates = []
            for key, info in trade_by_entry.items():
                try:
                    e_dt = to_utc(datetime.fromisoformat(key.replace("Z", "+00:00")))
                except (ValueError, TypeError):
                    continue
                if e_dt >= ts_dt and (e_dt - ts_dt).total_seconds() <= 300:
                    candidates.append((e_dt, info))
            if candidates:
                linked = min(candidates, key=lambda x: x[0])[1]
        if linked is not None:
            used_trades.add(linked["trade_id"])
            terminal = "OPEN_AT_END" if linked.get("open_at_end") else "EXECUTED_TRADE"
            life.append({"intent_id": intent_id, "decision_time": ts, "side": side,
                         "terminal": terminal, "raw_reason": linked["exit_reason"],
                         "reason_detail": linked["exit_reason"],
                         "linked_trade_id": linked["trade_id"],
                         "episode_id": a.get("quorum_event_id"),
                         "fill_ts": ts, "open_at_end": linked.get("open_at_end", False)})
            continue
        # 2) точная причина из аудита движка
        term = rej_map.get((ts, side)) or rej_map.get((ts, "?")) or "LINKAGE_ERROR"
        raw_reason = term
        # 3) session: вне main (движок после фикса) — приоритетнее
        if ts_dt is not None:
            try:
                if session_at(ts_dt) != "main":
                    term = "REJECTED_SESSION"
                    raw_reason = "session_at_decision != main"
            except (ValueError, TypeError):
                pass
        # 4) нет следующего бара (no next available 1m bar)
        if term not in ("REJECTED_SESSION", "REJECTED_COOLDOWN", "REJECTED_IN_POSITION"):
            if ts_dt is not None:
                next_bar = next((c for c in candles if to_utc(c.ts) > ts_dt), None)
                if next_bar is None:
                    term = "REJECTED_NO_NEXT_BAR"
                    raw_reason = "no next available 1m bar"
        # 5) qty/лот: цена слишком высока для капитала
        if term == "LINKAGE_ERROR" and ts_dt is not None:
            next_bar = next((c for c in candles if to_utc(c.ts) > ts_dt), None)
            if next_bar is not None:
                fill = next_bar.open * (1 + float(req.get("slippage_bps", 2.0)) / 10000)
                q = qty_for_fill(capital, fill, lot)
                if q <= 0:
                    term = "REJECTED_PRICE_OR_LOT"
                    raw_reason = f"qty=0 at fill {fill:.2f}, capital {capital:.0f}, lot {lot}"
                elif fill * q > capital:
                    term = "REJECTED_CAPITAL"
                    raw_reason = f"notional {fill*q:.0f} > capital {capital:.0f}"
        # 6) duplicate episode: тот же side + quorum_event_id уже обработан
        if term == "LINKAGE_ERROR":
            seen_ep = set()
            for prev in life:
                if prev["side"] == side and prev.get("episode_id") and prev["episode_id"] == a.get("quorum_event_id"):
                    seen_ep.add(prev["intent_id"])
            if seen_ep:
                term = "REJECTED_DUPLICATE_EPISODE"
                raw_reason = f"duplicate episode {a.get('quorum_event_id')}"
        # 7) accepted, но без сделки и без reject-аудита -> CANCELLED_BEFORE_FILL
        if term == "LINKAGE_ERROR":
            term = "CANCELLED_BEFORE_FILL"
            raw_reason = "accepted intent без сделки и без reject-аудита движка"
        life.append({"intent_id": intent_id, "decision_time": ts, "side": side,
                     "terminal": term, "raw_reason": raw_reason,
                     "reason_detail": None, "linked_trade_id": None,
                     "episode_id": a.get("quorum_event_id"),
                     "fill_ts": None, "open_at_end": False})
    summary = Counter(x["terminal"] for x in life)
    return life, {"terminal_counts": dict(summary), "n_intents": len(life),
                  "n_trades": len(ledger.trades),
                  "n_linked": len(used_trades),
                  "unlinked_trades": len(ledger.trades) - len(used_trades),
                  "open_at_end_trades": sum(1 for x in life if x.get("open_at_end"))}


# ---------------------------------------------------------------------------
# MTM equity (1m mark-to-market): cash + realised + unrealised открытых позиций
# ---------------------------------------------------------------------------

def mtm_equity_1m(
    candles: list[Any],
    trades: list[dict],
    capital: float = 10_000,
) -> dict:
    """1m mark-to-market equity портфеля по одной FIGI.

    Для каждого закрытого 1m бара:
      equity[t] = capital + sum(realised net закрытых сделок до t)
                  + sum(unrealised P&L открытых позиций по close[t])

    Реализация: идём по барам; открываем позицию при entry_time, закрываем
    при exit_time. unrealised = (close - entry_fill) * qty для LONG.
    Открытые на конец — остаются со статусом OPEN_AT_END (включены в equity).

    Возвращает {bars: [{ts, equity, cash, realised, unrealised, open_positions}],
                mtm_drawdown_rub, mtm_drawdown_pct, realised_only: False}
    """
    bars: list[dict] = []
    realised = 0.0
    # открытые позиции: {entry_ts: {qty, entry_fill, side}}
    open_pos: dict[str, dict] = {}
    cum_peak = capital
    max_dd = 0.0
    for bar in candles:
        bt = to_utc(bar.ts)
        # закрываем сделки, чей exit_time == bar.ts (реализованный P&L)
        for t in trades:
            ex = t.get("exit_time") or t.get("exit_ts")
            if not ex:
                continue
            try:
                xd = to_utc(datetime.fromisoformat(str(ex).replace("Z", "+00:00")))
            except (ValueError, TypeError):
                continue
            if xd == bt:
                realised += float(t.get("net_rub") or t.get("net") or 0)
                # удаляем из открытых по entry
                e_key = str(t.get("entry_time") or t.get("entry_ts"))
                open_pos.pop(e_key, None)
        # открываем позиции, чей entry_time == bar.ts
        for t in trades:
            en = t.get("entry_time") or t.get("entry_ts")
            if not en:
                continue
            try:
                ed = to_utc(datetime.fromisoformat(str(en).replace("Z", "+00:00")))
            except (ValueError, TypeError):
                continue
            if ed == bt:
                side = str(t.get("side") or t.get("ticker_side") or "LONG").upper()
                qty = float(t.get("qty") or 0)
                fill = float(t.get("entry_fill_price") or t.get("entry_price") or 0)
                open_pos[str(en)] = {"qty": qty, "entry_fill": fill,
                                     "side": "LONG" if side in ("LONG", "BUY") else "SHORT"}
        # unrealised по close бара
        unreal = 0.0
        for p in open_pos.values():
            if p["qty"] <= 0:
                continue
            if p["side"] == "LONG":
                unreal += (bar.close - p["entry_fill"]) * p["qty"]
            else:
                unreal += (p["entry_fill"] - bar.close) * p["qty"]
        eq = capital + realised + unreal
        cum_peak = max(cum_peak, eq)
        max_dd = max(max_dd, cum_peak - eq)
        bars.append({
            "ts": bt.isoformat(),
            "equity": round(eq, 2),
            "cash": round(capital + realised, 2),
            "realised": round(realised, 2),
            "unrealised": round(unreal, 2),
            "open_positions": len(open_pos),
        })
    return {
        "bars": bars,
        "mtm_drawdown_rub": round(max_dd, 2),
        "mtm_drawdown_pct": round(max_dd / max(capital, 1e-9) * 100, 3) if capital else None,
        "realised_only": False,
        "final_equity": bars[-1]["equity"] if bars else capital,
        "final_open_positions": len(open_pos),
    }


# ---------------------------------------------------------------------------
# Session boundary audit: детали сделок у/за границей main
# ---------------------------------------------------------------------------

def session_boundary_audit(trades: list[dict]) -> dict:
    """Для сделок с entry/exit вне main или boundary fills — полные поля:
    decision_time_utc/msk, execution_time_utc/msk, session_at_decision,
    session_at_execution, allow_fill_after_main_boundary, terminal reason.
    """
    rows: list[dict] = []
    for t in trades:
        ed = None
        xd = None
        try:
            ed = to_utc(datetime.fromisoformat(str(t.get("entry_time") or t.get("entry_ts")).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            pass
        try:
            xd = to_utc(datetime.fromisoformat(str(t.get("exit_time") or t.get("exit_ts")).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            pass
        if ed is None and xd is None:
            continue
        s_in = session_at(ed) if ed else None
        s_ex = session_at(xd) if xd else None
        # только интересные: вход/выход вне main, или решение в main, выход позже
        interesting = (s_in != "main") or (s_ex != "main") or \
                      (s_in == "main" and s_ex == "outside")
        if not interesting:
            continue
        rows.append({
            "trade_id": t.get("trade_id") or t.get("figi"),
            "figi": t.get("figi"),
            "side": t.get("side"),
            "decision_time_utc": t.get("signal_time") or (ed.isoformat() if ed else None),
            "decision_time_msk": (datetime.fromisoformat(str(t.get("signal_time")).replace("Z", "+00:00")).astimezone(MSK).isoformat()
                                  if t.get("signal_time") else (ed.astimezone(MSK).isoformat() if ed else None)),
            "execution_time_utc": ed.isoformat() if ed else None,
            "execution_time_msk": ed.astimezone(MSK).isoformat() if ed else None,
            "exit_time_utc": xd.isoformat() if xd else None,
            "exit_time_msk": xd.astimezone(MSK).isoformat() if xd else None,
            "session_at_decision": s_in,
            "session_at_execution": s_ex,
            "allow_fill_after_main_boundary": bool(s_in == "main" and s_ex != "main"),
            "terminal_reason": t.get("exit_reason"),
        })
    return {
        "rows": rows,
        "count": len(rows),
        "note": "все сделки с входом/выходом вне main или с заполнением после границы",
    }
