#!/usr/bin/env python3
"""C3_TREND60_V1: причинное решение по первым шести 10min-барам часа 10:00-10:59.

Контракт фиксирован ДО рыночного расчёта (spec + TREND_SPEC_HASH ниже). Никаких
порогов «по результату», индикаторного голосования или настроек: одно правило.

Решение в 11:00 MSK по шести ПОЛНЫМ 10min-барам 10:00..10:50 (каждый = ровно 10
различных валидных 1m; неполный/отсутствующий час -> insufficient_context, НЕ
no_signal):
    close(10:50) > mean(close(10:00..10:50)) + FLOAT_EPS  -> long
    close(10:50) < mean(close(10:00..10:50)) - FLOAT_EPS  -> short
    иначе (равенство в пределах FLOAT_EPS)                -> no_signal (skip)

Один сигнал на ticker/day. ATR = существующий a7.atr_before(tdata, label 10:50):
последний полный сессионный 10min-бар СТРОГО до метки 10:50 — решающий бар 10:50
в ATR НЕ входит (atr_excludes_deciding_bar=True). Нет 14 TR -> atr_warmup_insufficient;
ATR <= 0 / не конечный -> atr_invalid; оба -> insufficient_context.

Вход = open минуты 11:00 MSK; decision_close_time == intended_entry_time == 11:00
(next-open модель: решение после закрытия бара 10:50-10:59, исполнение на открытии
следующей минуты 11:00; задержка исполнения НЕ моделируется). Поля события в UTC
(tz-aware), MSK используется только для выбора сессионных границ/меток.

Исполнение и расчёт — существующий A24 `execute_candidate_signal` через общее ядро
`app.engine.research_execution` (walk_exit / settle_entry / settle_exit): SL = 2 ATR,
TP = 3 ATR, плановый выход 18:30, правила пропусков пути — как в A7/A24. Новый
walk/settlement НЕ вводится; SL/TP-множители берутся из a7.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timezone

import day_strategies_core as a7

CANDIDATE_ID = "C3"
DECISION_LABEL = "10:50"
ENTRY_LABEL = "11:00"
HOUR_LABELS = ("10:00", "10:10", "10:20", "10:30", "10:40", "10:50")
HOUR_BAR_MINUTES = 10
SL_ATR_MULT = a7.SL_ATR_MULT
TP_ATR_MULT = a7.TP_ATR_MULT

SPEC = {
    "candidate_id": CANDIDATE_ID,
    "name": "C3_TREND60_V1",
    "decision_msk": ENTRY_LABEL,
    "decision_label": DECISION_LABEL,
    "entry_label": ENTRY_LABEL,
    "hour_labels_msk": list(HOUR_LABELS),
    "hour_bar_minutes": HOUR_BAR_MINUTES,
    "hour_minutes_required": 10,
    "rule": ("close(10:50) > mean(close(10:00..10:50)) -> long; "
             "< -> short; equal -> no_signal"),
    "signals_per": "ticker/day (one)",
    "incomplete_hour_policy": "insufficient_context (never no_signal)",
    "atr_source": "a7.atr_before(tdata, label_ts(day,'10:50'))",
    "atr_excludes_deciding_bar": True,
    "atr_boundary": "last full session 10min bar strictly before label 10:50",
    "atr_warmup_tr": a7.WARMUP_TR,
    "atr_invalid_policy": "insufficient_context",
    "sl_atr_mult": 2.0,
    "tp_atr_mult": 3.0,
    "sl_formula": "entry - side*2*atr",
    "tp_formula": "entry + side*3*atr",
    "entry_fill": ("open(11:00 MSK) == decision_close == intended_entry_time; "
                   "next-open; execution delay not modeled"),
    "session_boundaries": "MSK for day/labels; event timestamps UTC-aware",
    "exit_policy": ("existing A24/_walk via shared research_execution; path-skip "
                    "rules preserved; time-exit 18:30"),
    "costs": "existing CostParams/A24; no new walk/settlement",
    "float_eps": a7.FLOAT_EPS,
    "phase": "development_unvalidated",
    "holdout": "NOT_ASSIGNED",
}

TREND_SPEC_HASH = hashlib.sha256(
    json.dumps(SPEC, sort_keys=True, ensure_ascii=False,
               separators=(",", ":")).encode("utf-8")).hexdigest()


def spec_hash() -> str:
    return TREND_SPEC_HASH


def _base(ticker: str, day, observed_until, last_bar_open_time, spec_hash: str) -> dict:
    return {
        "type": "insufficient_context",
        "candidate_id": CANDIDATE_ID,
        "ticker": ticker,
        "day": day.isoformat(),
        "observed_until": observed_until.astimezone(timezone.utc).isoformat(),
        "last_bar_open_time": last_bar_open_time.astimezone(timezone.utc).isoformat(),
        "spec_hash": spec_hash,
    }


def _skip(base: dict, reason: str, detail: str) -> dict:
    out = dict(base)
    out["type"] = "skip"
    out["reason"] = reason
    out["detail"] = detail
    return out


def _insufficient(base: dict, reason: str, detail: str) -> dict:
    out = dict(base)
    out["type"] = "insufficient_context"
    out["reason"] = reason
    out["detail"] = detail
    return out


def evaluate_trend_day(tdata: a7.TickerData, day, *, spec_hash: str = None) -> dict:
    """Один день -> событие существующей формы `signal` | `skip` | `insufficient_context`.

    Читает только полные 10min-бары 10:00..10:50 и ATR строго до метки 10:50;
    будущие минуты после 11:00 на решение НЕ влияют.
    """
    sh = spec_hash or TREND_SPEC_HASH
    ticker = tdata.ticker
    decision_close = a7.label_ts(day, ENTRY_LABEL)
    last_bar_open = a7.label_ts(day, DECISION_LABEL)
    base = _base(ticker, day, decision_close, last_bar_open, sh)

    if day.weekday() >= 5:
        return _skip(base, "weekend_excluded",
                     "research evaluates weekday main session only")

    bars = []
    for lab in HOUR_LABELS:
        bar = a7._full_bar(tdata, day, lab)
        if bar is None:
            return _insufficient(
                base, "incomplete_hour",
                f"нет полного 10min-бара {lab} (нужно ровно 10 различных валидных 1m)")
        bars.append(bar)

    closes = [b.close for b in bars]
    mean_close = sum(closes) / len(closes)
    last_close = closes[-1]
    if last_close > mean_close + a7.FLOAT_EPS:
        side_sign = 1
    elif last_close < mean_close - a7.FLOAT_EPS:
        side_sign = -1
    else:
        return _skip(base, "no_signal",
                     f"close(10:50)={last_close!r} == mean={mean_close!r} "
                     f"в пределах FLOAT_EPS={a7.FLOAT_EPS!r}")

    atr, n_tr = a7.atr_before(tdata, last_bar_open)
    if n_tr < a7.WARMUP_TR:
        return _insufficient(base, "atr_warmup_insufficient",
                             f"TR до метки 10:50: {n_tr} < {a7.WARMUP_TR}")
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return _insufficient(base, "atr_invalid", f"atr={atr!r} n_tr={n_tr}")

    dc_iso = decision_close.astimezone(timezone.utc).isoformat()
    out = dict(base)
    out.update({
        "type": "signal",
        "decision_label": DECISION_LABEL,
        "decision_close_time": dc_iso,
        "intended_entry_time": dc_iso,
        "side": "long" if side_sign > 0 else "short",
        "atr": atr,
        "gates": {
            "hour_closes": closes,
            "hour_mean": mean_close,
            "decision_close": last_close,
            "side_sign": side_sign,
        },
        "entry_formula": "open(intended_entry_time)",
        "stop_loss_formula": f"entry - side * {SL_ATR_MULT} * atr",
        "take_profit_formula": f"entry + side * {TP_ATR_MULT} * atr",
    })
    return out
