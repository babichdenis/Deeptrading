#!/usr/bin/env python3
"""BATCH-15 group A (B1): кандидаты B01–B05.

Заранее выбранные ценовые гипотезы (не оптимизированные параметры). Общее
исполнение/риск/тайминг зафиксированы (A24/research_execution): SL=2ATR, TP=3ATR,
time-exit 18:30, пропуски пути A7. Один сигнал на ticker/day/кандидат; ровно одна
точка решения. ATR = a7.atr_before строго до метки последнего решающего бара.
Неполное обязательное окно / нет ATR -> insufficient_context (НЕ no_signal).
Равенство в сравнении -> no_signal (FLOAT_EPS, не подбирается).

B01–B02: решение 11:00 (после закрытия бара 10:50). Окно — бары 10:00 и 10:50.
B03–B05: решение 11:00. Окно — полные 10min-бары 10:00..10:50.
"""
from __future__ import annotations

import hashlib
import json
import math

import day_strategies_core as a7
import batch15_common as b15

CANDIDATE_IDS = ("B01", "B02", "B03", "B04", "B05")
GROUP = "B1"
DECISION_LABEL = "10:50"
ENTRY_LABEL = "11:00"

OR_LABELS = ("10:00", "10:10", "10:20")
MID_LABELS = ("10:30", "10:40")
B1050 = "10:50"
FULL_LABELS = OR_LABELS + MID_LABELS + (B1050,)

COMMON_SPEC = {
    "group": GROUP,
    "decision_msk": ENTRY_LABEL,
    "decision_label": DECISION_LABEL,
    "entry_label": ENTRY_LABEL,
    "window_labels_msk": list(FULL_LABELS),
    "window_bar_minutes": 10,
    "window_bars_required": len(FULL_LABELS),
    "rule_family": "price_action_breakout_revert_location",
    "atr_source": "a7.atr_before(tdata, label_ts(day,'10:50'))",
    "atr_excludes_deciding_bar": True,
    "atr_boundary": "last full session 10min bar strictly before label 10:50",
    "atr_warmup_tr": b15.WARMUP_TR,
    "atr_invalid_policy": "insufficient_context",
    "sl_atr_mult": b15.SL_ATR_MULT,
    "tp_atr_mult": b15.TP_ATR_MULT,
    "sl_formula": "entry - side*2*atr",
    "tp_formula": "entry + side*3*atr",
    "entry_fill": ("open(11:00 MSK) == decision_close == intended_entry_time; "
                   "next-open; execution delay not modeled"),
    "session_boundaries": "MSK for day/labels; event timestamps UTC-aware",
    "exit_policy": ("existing A24/_walk via shared research_execution; path-skip "
                    "rules preserved; time-exit 18:30"),
    "costs": "existing CostParams/A24; no new walk/settlement",
    "float_eps": b15.FLOAT_EPS,
    "phase": "development_unvalidated",
    "holdout": "NOT_ASSIGNED",
}

RULES = {
    "B01": ("MOMENTUM_1H_V1",
            "решение11:00; score=C11/O10-1; score>0 -> long, <0 -> short, "
            "=0 -> no_signal"),
    "B02": ("FADE_1H_V1",
            "решение11:00; score=-(C11/O10-1); score>0 -> long, <0 -> short, "
            "=0 -> no_signal"),
    "B03": ("OR30_BREAKOUT_V1",
            "решение11:00; диапазон10:00..10:29 (H,L из баров10:00/10:10/10:20); "
            "C11>H -> long, C11<L -> short, иначе no_signal; окно10:00..10:50 полное"),
    "B04": ("OR30_FALSE_BREAK_V1",
            "решение11:00; диапазон10:00..10:29 (H,L); в10:30..10:49 был high>H и "
            "C11 обратно внутри [L,H] -> short; был low<L и C11 внутри -> long; "
            "оба пробоя или ни одного -> no_signal; окно10:00..10:50 полное"),
    "B05": ("CLOSE_LOCATION_V1",
            "решение11:00; range10:00..10:50 (H,L); p=(C11-L)/(H-L); p>0.8 -> long, "
            "p<0.2 -> short, иначе no_signal; H==L -> no_signal"),
}


def _spec_for(cid: str) -> dict:
    name, rule = RULES[cid]
    spec = dict(COMMON_SPEC)
    spec.update({
        "candidate_id": cid,
        "name": f"{cid}_{name}",
        "rule": rule,
    })
    return spec


SPECS = {cid: _spec_for(cid) for cid in CANDIDATE_IDS}


def _hash(spec: dict) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


for _cid in CANDIDATE_IDS:
    SPECS[_cid]["spec_hash"] = _hash(SPECS[_cid])

SPEC_HASHES = {cid: SPECS[cid]["spec_hash"] for cid in CANDIDATE_IDS}

__all__ = ["CANDIDATE_IDS", "GROUP", "DECISION_LABEL", "ENTRY_LABEL",
           "SPECS", "SPEC_HASHES", "evaluate"]


# ------------------------------------------------------------------ helpers

def _momentum(bars_by_label):
    o10 = bars_by_label["10:00"].open
    c11 = bars_by_label["10:50"].close
    if not math.isfinite(o10) or o10 <= 0:
        return None, f"O10={o10!r} невалидна"
    return c11 / o10 - 1.0, None


# ------------------------------------------------------------------ evaluate

def _evaluate_one(cid: str, tdata: a7.TickerData, day, spec_hash: str) -> dict:
    ticker = tdata.ticker
    base = b15.event_base(cid, ticker, day, DECISION_LABEL, ENTRY_LABEL, spec_hash)
    if day.weekday() >= 5:
        return b15.mark_skip(base, "weekend_excluded",
                             "research evaluates weekday main session only")

    bars, missing = b15.gather_full_bars(tdata, day, FULL_LABELS)
    if bars is None:
        return b15.mark_insufficient(
            base, "incomplete_window",
            f"нет полного 10min-бара {missing} (нужно {len(FULL_LABELS)} "
            f"полных баров 10:00..10:50)")

    atr, n_tr = b15.atr_for_decision(tdata, day, DECISION_LABEL)
    if n_tr < b15.WARMUP_TR:
        return b15.mark_insufficient(
            base, "atr_warmup_insufficient",
            f"TR до метки 10:50: {n_tr} < {b15.WARMUP_TR}")
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return b15.mark_insufficient(base, "atr_invalid", f"atr={atr!r} n_tr={n_tr}")

    labels = {lab: b for lab, b in zip(FULL_LABELS, bars)}

    if cid in ("B01", "B02"):
        mom, err = _momentum(labels)
        if mom is None:
            return b15.mark_insufficient(base, "invalid_open", err)
        score = mom if cid == "B01" else -mom
        if b15.gt(score, 0.0):
            side = 1
        elif b15.lt(score, 0.0):
            side = -1
        else:
            return b15.mark_skip(base, "no_signal", f"score={score!r} == 0 (eps)")
        gates = {"o10": labels["10:00"].open, "c11": labels["10:50"].close,
                 "score": score,
                 "rule": "C11/O10-1" if cid == "B01" else "-(C11/O10-1)"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    or_bars = [labels["10:00"], labels["10:10"], labels["10:20"]]
    H = max(b.high for b in or_bars)
    L = min(b.low for b in or_bars)
    c11 = labels["10:50"].close

    if cid == "B03":
        if b15.gt(c11, H):
            side = 1
        elif b15.lt(c11, L):
            side = -1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"C11={c11!r} не пробил [L={L!r}, H={H!r}]")
        gates = {"H": H, "L": L, "c11": c11, "rule": "C11 breakout OR30"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    if cid == "B04":
        mid_bars = [labels["10:30"], labels["10:40"]]
        broke_high = any(b15.gt(b.high, H) for b in mid_bars)
        broke_low = any(b15.lt(b.low, L) for b in mid_bars)
        if broke_high and broke_low:
            return b15.mark_skip(base, "no_signal",
                                 "оба пробоя диапазона (ambiguous)")
        if broke_high:
            if b15.ge(c11, L) and b15.le(c11, H):
                gates = {"H": H, "L": L, "c11": c11,
                         "rule": "false_break_down (up-probe reverts inside)"}
                return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL,
                                       -1, atr, gates)
            return b15.mark_skip(base, "no_signal",
                                 f"пробой вверх, но C11={c11!r} вне [L,H]")
        if broke_low:
            if b15.ge(c11, L) and b15.le(c11, H):
                gates = {"H": H, "L": L, "c11": c11,
                         "rule": "false_break_up (down-probe reverts inside)"}
                return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL,
                                       +1, atr, gates)
            return b15.mark_skip(base, "no_signal",
                                 f"пробой вниз, но C11={c11!r} вне [L,H]")
        return b15.mark_skip(base, "no_signal", "пробоев не было")

    if cid == "B05":
        Hw = max(b.high for b in bars)
        Lw = min(b.low for b in bars)
        rng = Hw - Lw
        if not b15.gt(rng, 0.0):
            return b15.mark_skip(base, "no_signal",
                                 f"H==L={Hw!r} (range=0, eps)")
        p = (c11 - Lw) / rng
        if b15.gt(p, 0.8):
            side = 1
        elif b15.lt(p, 0.2):
            side = -1
        else:
            return b15.mark_skip(base, "no_signal", f"p={p!r} в (0.2,0.8)")
        gates = {"H": Hw, "L": Lw, "c11": c11, "p": p,
                 "rule": "close location in range"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    raise ValueError(f"unhandled candidate_id {cid!r}")


def evaluate(candidate_id: str, tdata: a7.TickerData, day) -> dict:
    """Один день -> событие существующей A24-формы signal|skip|insufficient_context."""
    if candidate_id not in SPECS:
        raise ValueError(f"unknown candidate_id {candidate_id!r} (нет в {CANDIDATE_IDS})")
    return _evaluate_one(candidate_id, tdata, day, SPECS[candidate_id]["spec_hash"])
