#!/usr/bin/env python3
"""BATCH-15 group B (B2): кандидаты B06–B10.

Каждый кандидат — заранее выбранное ПРАВИЛО (не оптимизированные параметры). Общее
исполнение/риск/тайминг зафиксированы (A24/research_execution): SL=2ATR, TP=3ATR,
time-exit 18:30, пропуски пути A7. Один сигнал на ticker/day/кандидат; ровно одна
точка решения. Решение на полном баре 11:50, вход = open 12:00 MSK (next-open, после
закрытия бара 11:50–11:59). ATR = a7.atr_before строго до метки 11:50 (решающий бар
11:50 в ATR НЕ входит). Неполное окно / нет ATR -> insufficient_context (НЕ no_signal).
Равенство в сравнении -> no_signal (FLOAT_EPS, не подбирается).

Окно из 12 ПОЛНЫХ 10min-баров 10:00..11:50 (каждый = ровно 10 различных валидных 1m).
H/L окна — max high / min low всех полных 10min-баров окна; типичная цена=(H+L+C)/3;
VWAP = sum(typical*volume)/sum(volume); volume = сумма 1m volume. Сумма volume=0 ->
insufficient_context.
"""
from __future__ import annotations

import hashlib
import json
import math

import day_strategies_core as a7
import batch15_common as b15

CANDIDATE_IDS = ("B06", "B07", "B08", "B09", "B10")
GROUP = "B2"
DECISION_LABEL = "11:50"
ENTRY_LABEL = "12:00"
WINDOW_LABELS = (
    "10:00", "10:10", "10:20", "10:30", "10:40", "10:50",
    "11:00", "11:10", "11:20", "11:30", "11:40", "11:50",
)
WINDOW_BARS_REQUIRED = len(WINDOW_LABELS)

COMMON_SPEC = {
    "group": GROUP,
    "decision_msk": ENTRY_LABEL,
    "decision_label": DECISION_LABEL,
    "entry_label": ENTRY_LABEL,
    "window_labels_msk": list(WINDOW_LABELS),
    "window_bar_minutes": 10,
    "window_bars_required": WINDOW_BARS_REQUIRED,
    "rule_family": "single_decision_open_window_vwap_ema_rsi_range",
    "atr_source": "a7.atr_before(tdata, label_ts(day,'11:50'))",
    "atr_excludes_deciding_bar": True,
    "atr_boundary": "last full session 10min bar strictly before label 11:50",
    "atr_warmup_tr": b15.WARMUP_TR,
    "atr_invalid_policy": "insufficient_context",
    "sl_atr_mult": b15.SL_ATR_MULT,
    "tp_atr_mult": b15.TP_ATR_MULT,
    "sl_formula": "entry - side*2*atr",
    "tp_formula": "entry + side*3*atr",
    "entry_fill": ("open(12:00 MSK) == decision_close == intended_entry_time; "
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
    "B06": ("VWAP_TREND_V1",
            "решение12:00; score=C12-VWAP(10:00..11:59); score>0 -> long, <0 -> short, "
            "=0 -> no_signal"),
    "B07": ("VWAP_REVERT_V1",
            "решение12:00; score=VWAP(10:00..11:59)-C12; score>0 -> long, <0 -> short, "
            "=0 -> no_signal"),
    "B08": ("EMA_CROSS_V1",
            "решение12:00; EMA3/EMA6 по 12 closes 10:00..11:50 (seed=первый close, "
            "alpha=2/(n+1)); EMA3 пересекла EMA6 вверх на последнем баре -> long, "
            "вниз -> short, нет пересечения -> no_signal"),
    "B09": ("RSI_REVERT_V1",
            "решение12:00; RSI по последним 6 close-to-close изменениям 12 closes; "
            "RSI<30 -> long, >70 -> short, иначе no_signal"),
    "B10": ("RANGE_EXPANSION_V1",
            "решение12:00; range(11:50) > 1.5*среднего range(10:50..11:40) -> "
            "score=close(11:50)-open(11:50) (знак -> long/short), иначе no_signal; "
            "все 12 утренних баров обязательны"),
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


# ------------------------------------------------------------------ helpers

def _ema(values: list, n: int) -> list:
    """EMA со seed=первый элемент, alpha=2/(n+1). out[0]=values[0]."""
    alpha = 2.0 / (n + 1.0)
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(alpha * values[i] + (1.0 - alpha) * out[i - 1])
    return out


def _vwap(bars) -> float:
    num = 0.0
    den = 0.0
    for b in bars:
        typ = (b.high + b.low + b.close) / 3.0
        num += typ * b.volume
        den += b.volume
    if den <= 0:
        return float("nan")
    return num / den


def _rsi_from_closes(closes: list) -> float:
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    last6 = changes[-6:]
    gains = [max(c, 0.0) for c in last6]
    losses = [max(-c, 0.0) for c in last6]
    ag = sum(gains) / 6.0
    al = sum(losses) / 6.0
    if ag == 0.0 and al == 0.0:
        return 50.0
    if al == 0.0 and ag > 0.0:
        return 100.0
    if ag == 0.0 and al > 0.0:
        return 0.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


# ------------------------------------------------------------------ evaluate

def _evaluate_one(cid: str, tdata: a7.TickerData, day, spec_hash: str) -> dict:
    ticker = tdata.ticker
    base = b15.event_base(cid, ticker, day, DECISION_LABEL, ENTRY_LABEL, spec_hash)
    if day.weekday() >= 5:
        return b15.mark_skip(base, "weekend_excluded",
                             "research evaluates weekday main session only")

    bars, missing = b15.gather_full_bars(tdata, day, WINDOW_LABELS)
    if bars is None:
        return b15.mark_insufficient(
            base, "incomplete_window",
            f"нет полного 10min-бара {missing} (нужно {WINDOW_BARS_REQUIRED} "
            f"полных баров 10:00..11:50)")

    atr, n_tr = b15.atr_for_decision(tdata, day, DECISION_LABEL)
    if n_tr < b15.WARMUP_TR:
        return b15.mark_insufficient(
            base, "atr_warmup_insufficient",
            f"TR до метки 11:50: {n_tr} < {b15.WARMUP_TR}")
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return b15.mark_insufficient(base, "atr_invalid", f"atr={atr!r} n_tr={n_tr}")

    closes = [b.close for b in bars]

    if cid == "B06":
        c12 = closes[-1]
        vwap = _vwap(bars)
        if not math.isfinite(vwap):
            return b15.mark_insufficient(base, "vwap_volume_zero",
                                        "сумма volume окна = 0")
        score = c12 - vwap
        if b15.gt(score, 0.0):
            side = 1
        elif b15.lt(score, 0.0):
            side = -1
        else:
            return b15.mark_skip(base, "no_signal",
                                f"C12={c12!r} == VWAP={vwap!r} в пределах FLOAT_EPS")
        gates = {"c12": c12, "vwap": vwap, "score": score,
                 "rule": "C12 - VWAP"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    if cid == "B07":
        c12 = closes[-1]
        vwap = _vwap(bars)
        if not math.isfinite(vwap):
            return b15.mark_insufficient(base, "vwap_volume_zero",
                                        "сумма volume окна = 0")
        score = vwap - c12
        if b15.gt(score, 0.0):
            side = 1
        elif b15.lt(score, 0.0):
            side = -1
        else:
            return b15.mark_skip(base, "no_signal",
                                f"VWAP={vwap!r} == C12={c12!r} в пределах FLOAT_EPS")
        gates = {"c12": c12, "vwap": vwap, "score": score,
                 "rule": "VWAP - C12"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    if cid == "B08":
        ema3 = _ema(closes, 3)
        ema6 = _ema(closes, 6)
        e3_prev, e3_curr = ema3[-2], ema3[-1]
        e6_prev, e6_curr = ema6[-2], ema6[-1]
        if b15.le(e3_prev, e6_prev) and b15.gt(e3_curr, e6_curr):
            side = 1
        elif b15.ge(e3_prev, e6_prev) and b15.lt(e3_curr, e6_curr):
            side = -1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"нет пересечения EMA3/EMA6 на последнем баре "
                f"(e3_prev={e3_prev!r}, e6_prev={e6_prev!r}, "
                f"e3_curr={e3_curr!r}, e6_curr={e6_curr!r})")
        gates = {"ema3_prev": e3_prev, "ema3_curr": e3_curr,
                 "ema6_prev": e6_prev, "ema6_curr": e6_curr,
                 "rule": "EMA3xEMA6"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    if cid == "B09":
        rsi = _rsi_from_closes(closes)
        if b15.lt(rsi, 30.0):
            side = 1
        elif b15.gt(rsi, 70.0):
            side = -1
        else:
            return b15.mark_skip(
                base, "no_signal",
                f"RSI={rsi!r} не вне [30,70] (в пределах FLOAT_EPS)")
        gates = {"rsi": rsi, "rule": "RSI_revert"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    if cid == "B10":
        last = bars[-1]
        prev6 = bars[-7:-1]
        avg_range_prev = sum((b.high - b.low) for b in prev6) / len(prev6)
        range_last = last.high - last.low
        threshold = 1.5 * avg_range_prev
        if not b15.gt(range_last, threshold):
            return b15.mark_skip(
                base, "no_signal",
                f"range(11:50)={range_last!r} <= 1.5*avg_range_prev6="
                f"{threshold!r}")
        score = last.close - last.open
        if b15.gt(score, 0.0):
            side = 1
        elif b15.lt(score, 0.0):
            side = -1
        else:
            return b15.mark_skip(base, "no_signal",
                                f"close-open(11:50)={score!r} == 0 в пределах FLOAT_EPS")
        gates = {"range_last": range_last, "avg_range_prev6": avg_range_prev,
                 "threshold": threshold, "score": score,
                 "rule": "range_expansion_then_direction"}
        return b15.mark_signal(base, day, DECISION_LABEL, ENTRY_LABEL, side, atr, gates)

    raise ValueError(f"unhandled candidate_id {cid!r}")


def evaluate(candidate_id: str, tdata: a7.TickerData, day) -> dict:
    """Один день -> событие существующей A24-формы signal|skip|insufficient_context."""
    if candidate_id not in SPECS:
        raise ValueError(f"unknown candidate_id {candidate_id!r} (нет в {CANDIDATE_IDS})")
    return _evaluate_one(candidate_id, tdata, day, SPECS[candidate_id]["spec_hash"])
