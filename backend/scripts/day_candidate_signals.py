#!/usr/bin/env python3
"""A18: причинный генератор сигналов кандидатов C1/C2 (A17 canonical).

Только обработка ЗАКРЫТОГО минутного префикса: накопление полных 10min-баров,
причинный диапазон/VWAP-proxy, ATR14 строго до решения, reset. Без исполнения,
без PnL, без SL/TP-цен, без будущих минут. Переиспользованы чистые хелперы A7.

Выход: `signal` | `skip` | `insufficient_context`. В момент сигнала будущий entry
open неизвестен — отдаются только формулы/множители. Отсутствие entry-минуты
решает последующее исполнение, здесь — только намерение.
"""
from __future__ import annotations

import hashlib
import math
from datetime import timedelta, timezone
from pathlib import Path

from day_strategies_core import (
    WARMUP_TR,
    MinuteBar,
    TickerData,
    _full_bar,
    atr_before,
    build_ticker_data,
    label_mod,
    label_ts,
    msk_date,
)

SPEC_HASH = "2a1ecab0ce437f643bae2a541a7abc306728c2aa1347f78e6a7ed27ae876f9e7"
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.0
C2_K_DEV = 1.0
C2_K_SLOPE = 1.0
C2_SLOPE_MINUTES = 60
OR_LABELS = ("10:00", "10:10", "10:20")


def _scan_labels(start_hhmm: str, end_hhmm: str) -> tuple:
    sm = label_mod(start_hhmm)
    em = label_mod(end_hhmm)
    return tuple(f"{m // 60:02d}:{m % 60:02d}" for m in range(sm, em + 1, 10))


C1_SCAN = _scan_labels("10:30", "14:00")
C2_SCAN = _scan_labels("11:00", "14:00")


def _hhmm(mod: int) -> str:
    return f"{mod // 60:02d}:{mod % 60:02d}"


def _base(kind: str, candidate_id: str, ticker: str, day, observed_until, last_bar_open_time, spec_hash: str) -> dict:
    return {
        "type": kind,
        "candidate_id": candidate_id,
        "ticker": ticker,
        "day": day.isoformat(),
        "observed_until": observed_until.isoformat(),
        "last_bar_open_time": last_bar_open_time.isoformat(),
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


def _atr_gate(tdata: TickerData, decision_bar) -> tuple:
    atr, n_tr = atr_before(tdata, decision_bar.ts)
    if n_tr < WARMUP_TR:
        return None, ("atr_warmup_insufficient", f"n_tr={n_tr} < {WARMUP_TR}")
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None, ("atr_invalid", f"atr={atr!r} n_tr={n_tr}")
    return atr, None


def _signal(base: dict, lab: str, bar, atr: float, gates: dict) -> dict:
    decision_close = bar.ts + timedelta(minutes=10)
    out = dict(base)
    out.update(
        {
            "type": "signal",
            "decision_label": lab,
            "decision_close_time": decision_close.astimezone(timezone.utc).isoformat(),
            "intended_entry_time": decision_close.astimezone(timezone.utc).isoformat(),
            "side": "long" if gates["side_sign"] > 0 else "short",
            "atr": atr,
            "gates": {k: v for k, v in gates.items() if k != "side_sign"},
            "entry_formula": "open(intended_entry_time)",
            "stop_loss_formula": f"entry - side * {SL_ATR_MULT} * atr",
            "take_profit_formula": f"entry + side * {TP_ATR_MULT} * atr",
        }
    )
    return out


def _gather_prefix(tdata: TickerData, day, upto_lab: str):
    bars = []
    for m in range(label_mod("10:00"), label_mod(upto_lab) + 1, 10):
        lab = _hhmm(m)
        bar = _full_bar(tdata, day, lab)
        if bar is None:
            return bars, lab
        bars.append(bar)
    return bars, None


def _eval_c1(tdata: TickerData, base: dict, day, observed_until) -> dict:
    or_bars = []
    for lab in OR_LABELS:
        bar = _full_bar(tdata, day, lab)
        if bar is None:
            end = label_ts(day, lab) + timedelta(minutes=10)
            if observed_until >= end:
                return _skip(base, "prefix_gap", f"missing full OR bar {lab}")
            return _insufficient(base, "awaiting_or", f"OR bar {lab} not complete")
        or_bars.append(bar)
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)

    for lab in C1_SCAN:
        bar = _full_bar(tdata, day, lab)
        end = label_ts(day, lab) + timedelta(minutes=10)
        if bar is None:
            if observed_until >= end:
                return _skip(base, "prefix_gap", f"missing full bar {lab}")
            return _insufficient(base, "awaiting_bar", f"bar {lab} not complete")
        atr, gate_err = _atr_gate(tdata, bar)
        if gate_err is not None:
            return _skip(base, gate_err[0], gate_err[1])
        if bar.close > or_high:
            gates = {"or_high": or_high, "or_low": or_low, "decision_close": bar.close, "side_sign": 1}
            return _signal(base, lab, bar, atr, gates)
        if bar.close < or_low:
            gates = {"or_high": or_high, "or_low": or_low, "decision_close": bar.close, "side_sign": -1}
            return _signal(base, lab, bar, atr, gates)

    last_end = label_ts(day, "14:00") + timedelta(minutes=10)
    if observed_until >= last_end:
        return _skip(base, "no_breakout", "close within OR through 14:00")
    return _insufficient(base, "awaiting_window", "scan window not complete")


def _eval_c2(tdata: TickerData, base: dict, day, observed_until) -> dict:
    for lab in C2_SCAN:
        bar = _full_bar(tdata, day, lab)
        end = label_ts(day, lab) + timedelta(minutes=10)
        if bar is None:
            if observed_until >= end:
                return _skip(base, "prefix_gap", f"missing full bar {lab}")
            return _insufficient(base, "awaiting_bar", f"bar {lab} not complete")

        bars, miss = _gather_prefix(tdata, day, lab)
        if miss is not None:
            mend = label_ts(day, miss) + timedelta(minutes=10)
            if observed_until >= mend:
                return _skip(base, "prefix_gap", f"missing full bar {miss} in prefix to {lab}")
            return _insufficient(base, "awaiting_prefix", f"bar {miss} not complete")

        tot_v = sum(b.volume for b in bars)
        if tot_v <= 0:
            return _skip(base, "vwap_no_volume", f"cumulative volume 0 through {lab}")
        vwap = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars) / tot_v

        m60 = label_mod(lab) - C2_SLOPE_MINUTES
        lab60 = _hhmm(m60)
        bars60, miss60 = _gather_prefix(tdata, day, lab60)
        if miss60 is not None:
            return _skip(base, "prefix_gap", f"missing full bar {miss60} for t-60min of {lab}")
        tot_v60 = sum(b.volume for b in bars60)
        if tot_v60 <= 0:
            return _skip(base, "vwap_no_volume", f"cumulative volume 0 through {lab60}")
        vwap60 = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars60) / tot_v60

        atr, gate_err = _atr_gate(tdata, bar)
        if gate_err is not None:
            return _skip(base, gate_err[0], gate_err[1])

        slope = vwap - vwap60
        if abs(slope) >= C2_K_SLOPE * atr:
            continue
        dev = bar.close / vwap - 1.0
        threshold = C2_K_DEV * atr / vwap
        if abs(dev) >= threshold:
            gates = {
                "vwap": vwap,
                "vwap_t_minus_60": vwap60,
                "slope": slope,
                "deviation": dev,
                "threshold": threshold,
                "side_sign": -1 if dev > 0 else 1,
            }
            return _signal(base, lab, bar, atr, gates)

    last_end = label_ts(day, "14:00") + timedelta(minutes=10)
    if observed_until >= last_end:
        return _skip(base, "no_signal", "no reversion signal through 14:00")
    return _insufficient(base, "awaiting_window", "scan window not complete")


def _evaluate_day(tdata: TickerData, day, observed_until, last_bar_open_time, candidate_id: str, ticker: str, spec_hash: str) -> dict:
    base = _base("insufficient_context", candidate_id, ticker, day, observed_until, last_bar_open_time, spec_hash)
    if day.weekday() >= 5:
        return _skip(base, "weekend_excluded", "research evaluates weekday main session only")
    if candidate_id == "C1":
        return _eval_c1(tdata, base, day, observed_until)
    if candidate_id == "C2":
        return _eval_c2(tdata, base, day, observed_until)
    raise ValueError(f"unknown candidate_id: {candidate_id}")


class CandidateSignalGenerator:
    """Потоковый причинный генератор: feed(закрытая минута) -> первый event за день.

    Контракт закрытой минуты: ts — НАЧАЛО минуты; мутацию состояния делает только
    валидный бар (aware, выровненный по минуте, строго возрастающий/уникальный,
    конечные корректные OHLC, неотрицательный конечный volume). При невалидном
    баре — ValueError до мутации; дубль не может стать десятой минутой бакета.
    Полнота 10min-бара = 10 различных ожидаемых минут (уникальность гарантирует
    валидатор). observed_until = ts последнего бара + 1min UTC (бар целиком закрыт).
    """

    def __init__(self, candidate_id: str, ticker: str, warmup_start=None, spec_hash: str = SPEC_HASH):
        self.candidate_id = candidate_id
        self.ticker = ticker
        self.warmup_start = warmup_start
        self.spec_hash = spec_hash
        self.reset()

    def reset(self) -> None:
        self._minutes: list = []
        self._events: list = []
        self._done_days: set = set()

    def feed(self, bar: MinuteBar):
        self._validate_bar(bar)
        self._minutes.append(bar)
        return self.evaluate()

    def _validate_bar(self, bar: MinuteBar) -> None:
        ts = bar.ts
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("feed_ts_naive")
        if ts.second != 0 or ts.microsecond != 0:
            raise ValueError("feed_ts_not_minute_aligned")
        if self._minutes and ts <= self._minutes[-1].ts:
            raise ValueError("feed_ts_not_strictly_increasing")
        o, h, l, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            raise ValueError("feed_ohlc_invalid")
        if not (h >= l and h >= max(o, c) and l <= min(o, c)):
            raise ValueError("feed_ohlc_invalid")
        if not (math.isfinite(v) and v >= 0):
            raise ValueError("feed_volume_invalid")

    def inspect(self):
        """Текущее решение по последнему дню БЕЗ записи в events (может быть
        insufficient_context). Возвращает None, если день уже терминально решён."""
        if not self._minutes:
            return None
        last = self._minutes[-1]
        day = msk_date(last.ts)
        if day in self._done_days:
            return None
        issues: list = []
        warmup_start = self.warmup_start or msk_date(self._minutes[0].ts)
        tdata = build_ticker_data(self.ticker, self._minutes, warmup_start, issues)
        observed_until = last.ts + timedelta(minutes=1)
        return _evaluate_day(tdata, day, observed_until, last.ts,
                             self.candidate_id, self.ticker, self.spec_hash)

    def evaluate(self):
        event = self.inspect()
        if event is not None and event["type"] in ("signal", "skip"):
            self._done_days.add(msk_date(self._minutes[-1].ts))
            self._events.append(event)
            return event
        return None

    @property
    def events(self) -> list:
        return list(self._events)


def evaluate_prefix(minutes, candidate_id: str, ticker: str = "SYNTH", warmup_start=None,
                    spec_hash: str = SPEC_HASH) -> list:
    gen = CandidateSignalGenerator(candidate_id, ticker, warmup_start, spec_hash)
    for bar in minutes:
        gen.feed(bar)
    return gen.events


def canonical_spec_hash() -> str:
    path = Path(__file__).resolve().parents[2] / (
        "backend/reports/architect_20261010/b3/a17/architect_review_20261010T113712Z/candidates_canonical.json"
    )
    if not path.exists():
        return "NOT_AVAILABLE"
    return hashlib.sha256(path.read_bytes()).hexdigest()
