#!/usr/bin/env python3
"""BATCH-15 group C (B3): кандидаты B11–B15.

Каждый кандидат — заранее выбранное ПРАВИЛО (не оптимизированные параметры). Общее
исполнение/риск/тайминг зафиксированы (A24/research_execution): SL=2ATR, TP=3ATR,
time-exit 18:30, пропуски пути A7. Один сигнал на ticker/day/кандидат; ровно одна
точка решения. ATR = a7.atr_before строго до метки последнего решающего бара
(как C3; решающий бар в ATR НЕ входит). Неполное окно / нет ATR -> insufficient_context
(НЕ no_signal). Равенство в сравнении -> no_signal (FLOAT_EPS, не подбирается).

Обозначения окон (фикс B2): O10=open10:00; C11=close бара10:50; C12=close бара11:50;
C14=close бара13:50. H/L окна — max high/min low всех полных 10min-баров окна.

B11 MIDDAY_CONTINUATION 14:00: r1=C12/O10-1, r2=C14/C12-1; одинаковые ненулевые
    знаки -> score=r2, иначе no_signal. Окно 10:00..13:59 (24 бара) полное.
B12 MIDDAY_REVERSAL 14:00: r1/r2 как B11; противоположные знаки и
    abs(r2)>abs(r1)/2 -> score=r2, иначе no_signal. Окно 10:00..13:59 полное.
B13 INSIDE_BREAK 12:00: bar11:40 внутри bar11:30 (H<=Hprev, L>=Lprev);
    close11:50>H11:30 -> long; <L11:30 -> short; иначе no_signal. 12 утренних баров.
B14 THREE_BAR_EXHAUSTION 12:00: бары11:30/11:40/11:50 все bullish и closes строго
    растут -> short; все bearish и closes строго падают -> long; иначе no_signal.
B15 COMPRESSION_BREAK 14:00: range12:00..12:59 < 0.5*range11:00..11:59;
    C14>H12-окна -> long; C14<L12-окна -> short; иначе no_signal. 10:00..13:59 полное.
"""
from __future__ import annotations

import hashlib
import json
import math

import day_strategies_core as a7
import batch15_common as b15

CANDIDATE_IDS = ("B11", "B12", "B13", "B14", "B15")
GROUP = "C"

MORNING_LABELS = (
    "10:00", "10:10", "10:20", "10:30", "10:40", "10:50",
    "11:00", "11:10", "11:20", "11:30", "11:40", "11:50",
)
NOON_LABELS = ("12:00", "12:10", "12:20", "12:30", "12:40", "12:50")
AFTERNOON_LABELS = ("13:00", "13:10", "13:20", "13:30", "13:40", "13:50")
FULL_LABELS = MORNING_LABELS + NOON_LABELS + AFTERNOON_LABELS

DECISION_12 = "11:50"
ENTRY_12 = "12:00"
DECISION_14 = "13:50"
ENTRY_14 = "14:00"

COMMON_SPEC = {
    "group": GROUP,
    "entry_fill": ("open(entry MSK) == decision_close == intended_entry_time; "
                   "next-open; execution delay not modeled"),
    "session_boundaries": "MSK for day/labels; event timestamps UTC-aware",
    "exit_policy": ("existing A24/_walk via shared research_execution; path-skip "
                    "rules preserved; time-exit 18:30"),
    "costs": "existing CostParams/A24; no new walk/settlement",
    "sl_atr_mult": b15.SL_ATR_MULT,
    "tp_atr_mult": b15.TP_ATR_MULT,
    "sl_formula": "entry - side*2*atr",
    "tp_formula": "entry + side*3*atr",
    "atr_source": "a7.atr_before(tdata, label_ts(day, last_deciding_bar))",
    "atr_excludes_deciding_bar": True,
    "atr_warmup_tr": b15.WARMUP_TR,
    "atr_invalid_policy": "insufficient_context",
    "rule_family": "single_decision_window_sign_rules",
    "window_bar_minutes": 10,
    "float_eps": b15.FLOAT_EPS,
    "phase": "development_unvalidated",
    "holdout": "NOT_ASSIGNED",
}

SPECS: dict[str, dict] = {}


def _make_spec(cid: str, name: str, rule: str, decision_label: str,
               entry_label: str, window_labels: tuple[str, ...]) -> dict:
    spec = dict(COMMON_SPEC)
    spec.update({
        "candidate_id": cid,
        "name": name,
        "rule": rule,
        "decision_label": decision_label,
        "entry_label": entry_label,
        "decision_msk": entry_label,
        "window_labels_msk": list(window_labels),
        "window_bars_required": len(window_labels),
        "atr_boundary": (f"last full session 10min bar strictly before label "
                         f"{decision_label}"),
    })
    return spec


SPECS["B11"] = _make_spec(
    "B11", "B11_MIDDAY_CONTINUATION_V1",
    "решение14:00; r1=C12/O10-1, r2=C14/C12-1; одинаковые ненулевые знаки -> "
    "score=r2 (знак -> long/short), иначе no_signal; окно10:00..13:59 полное",
    DECISION_14, ENTRY_14, FULL_LABELS)

SPECS["B12"] = _make_spec(
    "B12", "B12_MIDDAY_REVERSAL_V1",
    "решение14:00; r1=C12/O10-1, r2=C14/C12-1; противоположные знаки и "
    "abs(r2)>abs(r1)/2 -> score=r2 (знак -> long/short), иначе no_signal; "
    "окно10:00..13:59 полное",
    DECISION_14, ENTRY_14, FULL_LABELS)

SPECS["B13"] = _make_spec(
    "B13", "B13_INSIDE_BREAK_V1",
    "решение12:00; bar11:40 внутри bar11:30 (H<=Hprev, L>=Lprev); "
    "close11:50>H11:30 -> long; close11:50<L11:30 -> short; иначе no_signal; "
    "все12 утренних баров полные",
    DECISION_12, ENTRY_12, MORNING_LABELS)

SPECS["B14"] = _make_spec(
    "B14", "B14_THREE_BAR_EXHAUSTION_V1",
    "решение12:00; бары11:30/11:40/11:50 все bullish(close>open) и closes строго "
    "растут -> short; все bearish и closes строго падают -> long; иначе no_signal; "
    "все12 полные",
    DECISION_12, ENTRY_12, MORNING_LABELS)

SPECS["B15"] = _make_spec(
    "B15", "B15_COMPRESSION_BREAK_V1",
    "решение14:00; range12:00..12:59 < 0.5*range11:00..11:59; при условии "
    "C14>H12:00..12:59 (окно) -> long; C14<L этого окна -> short; иначе no_signal; "
    "всё10:00..13:59 полное",
    DECISION_14, ENTRY_14, FULL_LABELS)


def _spec_hash(spec: dict) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


for _cid in CANDIDATE_IDS:
    SPECS[_cid]["spec_hash"] = _spec_hash(SPECS[_cid])

SPEC_HASHES = {cid: SPECS[cid]["spec_hash"] for cid in CANDIDATE_IDS}


# ------------------------------------------------------------------ evaluate

def _evaluate_one(cid: str, tdata: a7.TickerData, day, spec_hash: str) -> dict:
    ticker = tdata.ticker
    spec = SPECS[cid]
    dec_label = spec["decision_label"]
    ent_label = spec["entry_label"]
    window_labels = spec["window_labels_msk"]

    base = b15.event_base(cid, ticker, day, dec_label, ent_label, spec_hash)
    if day.weekday() >= 5:
        return b15.mark_skip(base, "weekend_excluded",
                             "research evaluates weekday main session only")

    bars, missing = b15.gather_full_bars(tdata, day, window_labels)
    if bars is None:
        return b15.mark_insufficient(
            base, "incomplete_window",
            f"нет полного 10min-бара {missing} (нужно {len(window_labels)} "
            f"полных баров)")

    atr, n_tr = b15.atr_for_decision(tdata, day, dec_label)
    if n_tr < b15.WARMUP_TR:
        return b15.mark_insufficient(
            base, "atr_warmup_insufficient",
            f"TR до метки {dec_label}: {n_tr} < {b15.WARMUP_TR}")
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return b15.mark_insufficient(base, "atr_invalid",
                                     f"atr={atr!r} n_tr={n_tr}")

    bar_by_label = {lab: b for lab, b in zip(window_labels, bars)}

    if cid in ("B11", "B12"):
        o10 = bar_by_label["10:00"].open
        c12 = bar_by_label["11:50"].close
        c14 = bar_by_label["13:50"].close
        r1 = c12 / o10 - 1.0
        r2 = c14 / c12 - 1.0
        if cid == "B11":
            if b15.gt(r1, 0.0) and b15.gt(r2, 0.0):
                side = 1
            elif b15.lt(r1, 0.0) and b15.lt(r2, 0.0):
                side = -1
            else:
                return b15.mark_skip(
                    base, "no_signal",
                    f"r1={r1!r} r2={r2!r}: знаки не одинаковые/ненулевые")
        else:
            if b15.gt(r1, 0.0) and b15.lt(r2, 0.0) and b15.gt(abs(r2), abs(r1) / 2.0):
                side = 1
            elif b15.lt(r1, 0.0) and b15.gt(r2, 0.0) and b15.gt(abs(r2), abs(r1) / 2.0):
                side = -1
            else:
                return b15.mark_skip(
                    base, "no_signal",
                    f"r1={r1!r} r2={r2!r}: нет разворотной комбинации с "
                    f"abs(r2)>abs(r1)/2")
        gates = {"o10": o10, "c12": c12, "c14": c14,
                 "r1": r1, "r2": r2, "score": r2}
        return b15.mark_signal(base, day, dec_label, ent_label, side, atr, gates)

    if cid == "B13":
        h1130 = bar_by_label["11:30"].high
        l1130 = bar_by_label["11:30"].low
        h1140 = bar_by_label["11:40"].high
        l1140 = bar_by_label["11:40"].low
        c1150 = bar_by_label["11:50"].close
        inside = b15.le(h1140, h1130) and b15.ge(l1140, l1130)
        if not inside:
            return b15.mark_skip(
                base, "no_signal",
                f"bar11:40 не внутри bar11:30 (H11:40={h1140!r} H11:30={h1130!r}, "
                f"L11:40={l1140!r} L11:30={l1130!r})")
        if b15.gt(c1150, h1130):
            side = 1
        elif b15.lt(c1150, l1130):
            side = -1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"C11={c1150!r} не пробил [L11:30={l1130!r}, H11:30={h1130!r}]")
        gates = {"h1130": h1130, "l1130": l1130, "h1140": h1140, "l1140": l1140,
                 "c1150": c1150, "inside": inside}
        return b15.mark_signal(base, day, dec_label, ent_label, side, atr, gates)

    if cid == "B14":
        trio = [bar_by_label[lab] for lab in ("11:30", "11:40", "11:50")]
        all_bull = all(b.close > b.open for b in trio)
        all_bear = all(b.close < b.open for b in trio)
        grows = b15.lt(trio[0].close, trio[1].close) and b15.lt(trio[1].close,
                                                               trio[2].close)
        falls = b15.gt(trio[0].close, trio[1].close) and b15.gt(trio[1].close,
                                                               trio[2].close)
        if all_bull and grows:
            side = -1
        elif all_bear and falls:
            side = 1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"нет экстремумного паттерна: bull={all_bull} bear={all_bear} "
                f"grows={grows} falls={falls} "
                f"closes={[b.close for b in trio]}")
        gates = {"closes": [b.close for b in trio],
                 "opens": [b.open for b in trio],
                 "all_bullish": all_bull, "all_bearish": all_bear,
                 "closes_grow": grows, "closes_fall": falls}
        return b15.mark_signal(base, day, dec_label, ent_label, side, atr, gates)

    if cid == "B15":
        bars11 = [bar_by_label[lab] for lab in MORNING_LABELS[6:]]
        bars12 = [bar_by_label[lab] for lab in NOON_LABELS]
        range11 = max(b.high for b in bars11) - min(b.low for b in bars11)
        range12 = max(b.high for b in bars12) - min(b.low for b in bars12)
        threshold = 0.5 * range11
        if not b15.lt(range12, threshold):
            return b15.mark_skip(
                base, "no_signal",
                f"range12={range12!r} >= 0.5*range11={threshold!r} "
                f"(нет сжатия)")
        h12 = max(b.high for b in bars12)
        l12 = min(b.low for b in bars12)
        c14 = bar_by_label["13:50"].close
        if b15.gt(c14, h12):
            side = 1
        elif b15.lt(c14, l12):
            side = -1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"C14={c14!r} не пробил окно12 [L12={l12!r}, H12={h12!r}]")
        gates = {"range11": range11, "range12": range12,
                 "threshold": threshold, "h12": h12, "l12": l12, "c14": c14}
        return b15.mark_signal(base, day, dec_label, ent_label, side, atr, gates)

    raise ValueError(f"unhandled candidate_id {cid!r}")


def evaluate(candidate_id: str, tdata: a7.TickerData, day) -> dict:
    """Один день -> событие существующей A24-формы signal|skip|insufficient_context."""
    if candidate_id not in SPECS:
        raise ValueError(f"unknown candidate_id {candidate_id!r} (нет в {CANDIDATE_IDS})")
    return _evaluate_one(candidate_id, tdata, day, SPECS[candidate_id]["spec_hash"])
