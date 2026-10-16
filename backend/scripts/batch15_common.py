#!/usr/bin/env python3
"""BATCH-15 shared helpers (B2). Contains ONLY: full-bar preparation, ATR, event fields.

Exit/settlement is NOT copied here — execution reuses A24 `execute_candidate_signal`
over the shared `research_execution` core (A7 `_walk` + `settle_entry`/`settle_exit`).
Группы a/b/c не импортируют друг друга; этот модуль — общий контракт B2.
"""
from __future__ import annotations

import math
from datetime import timezone

import day_strategies_core as a7

FLOAT_EPS = a7.FLOAT_EPS
SL_ATR_MULT = a7.SL_ATR_MULT
TP_ATR_MULT = a7.TP_ATR_MULT
WARMUP_TR = a7.WARMUP_TR


def gt(a: float, b: float) -> bool:
    """Строгое > с FLOAT_EPS (канон A7)."""
    return a > b + FLOAT_EPS


def lt(a: float, b: float) -> bool:
    """Строгое < с FLOAT_EPS."""
    return a < b - FLOAT_EPS


def ge(a: float, b: float) -> bool:
    """Нестрогое >= с FLOAT_EPS (a >= b заменяется на a >= b - FLOAT_EPS)."""
    return a >= b - FLOAT_EPS


def le(a: float, b: float) -> bool:
    """Нестрогое <= с FLOAT_EPS (a <= b заменяется на a <= b + FLOAT_EPS)."""
    return a <= b + FLOAT_EPS


def gather_full_bars(tdata: a7.TickerData, day, labels):
    """Все метки -> список Bar10 (каждый полный). (bars, None) либо (None, пропущенная_метка)."""
    bars = []
    for lab in labels:
        b = a7._full_bar(tdata, day, lab)
        if b is None:
            return None, lab
        bars.append(b)
    return bars, None


def atr_for_decision(tdata: a7.TickerData, day, decision_label: str):
    """ATR строго до метки последнего решающего бара (как C3/канон A7)."""
    return a7.atr_before(tdata, a7.label_ts(day, decision_label))


def event_base(cid: str, ticker: str, day, decision_label: str, entry_label: str,
               spec_hash: str) -> dict:
    decision_close = a7.label_ts(day, entry_label)
    last_bar_open = a7.label_ts(day, decision_label)
    return {
        "type": "insufficient_context",
        "candidate_id": cid,
        "ticker": ticker,
        "day": day.isoformat(),
        "observed_until": decision_close.astimezone(timezone.utc).isoformat(),
        "last_bar_open_time": last_bar_open.astimezone(timezone.utc).isoformat(),
        "spec_hash": spec_hash,
    }


def mark_skip(base: dict, reason: str, detail: str) -> dict:
    out = dict(base)
    out["type"] = "skip"
    out["reason"] = reason
    out["detail"] = detail
    return out


def mark_insufficient(base: dict, reason: str, detail: str) -> dict:
    out = dict(base)
    out["type"] = "insufficient_context"
    out["reason"] = reason
    out["detail"] = detail
    return out


def mark_signal(base: dict, day, decision_label: str, entry_label: str,
                side_sign: int, atr: float, gates: dict) -> dict:
    dc_iso = a7.label_ts(day, entry_label).astimezone(timezone.utc).isoformat()
    out = dict(base)
    out.update({
        "type": "signal",
        "decision_label": decision_label,
        "decision_close_time": dc_iso,
        "intended_entry_time": dc_iso,
        "side": "long" if side_sign > 0 else "short",
        "atr": atr,
        "gates": gates,
        "entry_formula": "open(intended_entry_time)",
        "stop_loss_formula": f"entry - side * {SL_ATR_MULT} * atr",
        "take_profit_formula": f"entry + side * {TP_ATR_MULT} * atr",
    })
    return out
