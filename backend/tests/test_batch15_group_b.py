"""BATCH-15 group B (B06–B10): постоянные synthetic-тесты (B2).

Герметично: синтетика в памяти, offline. Направления каждого кандидата, неполное
окно -> insufficient_context (НЕ no_signal), weekend -> skip, ATR warmup/недостаток,
равенство -> no_signal, причинность (будущие бары не влияют), точная минута входа
12:00, решающий бар 11:50, полная цепочка signal->A24->ledger, регистрация spec в
A24 и неизменность C1/C2, повторяемость evaluate.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_strategies_core as a7
import batch15_group_b as g
import batch15_common as b15
import day_candidate_execution as a24

W0 = date(2026, 6, 8)
W1 = date(2026, 6, 9)
W2 = date(2026, 6, 10)
D = date(2026, 6, 11)            # Thu
SAT = date(2026, 6, 13)          # Sat
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)
ABS_TOL = 1e-9


def _bars_day(day, price_fn, volume=10.0, skip_mods=()):
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod not in skip_mods:
            p = price_fn(mod, i)
            bars.append(a7.MinuteBar(tick, p, p + 0.01, p - 0.01, p, volume))
        tick += timedelta(minutes=1)
        i += 1
    return bars


def _warmup():
    return (_bars_day(W0, lambda m, i: 100.0) + _bars_day(W1, lambda m, i: 100.0)
            + _bars_day(W2, lambda m, i: 100.0))


def _window_bars(day, window):
    minutes = []
    for k, (o, h, l, c, v) in enumerate(window):
        base_mod = 600 + k * 10
        mid = (o + c) / 2.0
        for j in range(10):
            mod = base_mod + j
            ts = a7.label_ts(day, f"{mod // 60:02d}:{mod % 60:02d}")
            if j == 0:
                minutes.append(a7.MinuteBar(ts, o, h, l, o, v / 10.0))
            elif j == 9:
                minutes.append(a7.MinuteBar(ts, mid, h, l, c, v / 10.0))
            else:
                minutes.append(a7.MinuteBar(ts, mid, h, l, mid, v / 10.0))
    return minutes


def _tdata(window=None, decision_day=D, warmup_start=W0, skip_window_mod=None):
    bars = list(_warmup())
    if window is not None:
        wb = _window_bars(decision_day, window)
        if skip_window_mod is not None:
            wb = [b for b in wb if a7.msk_mod(b.ts) != skip_window_mod]
        bars += wb
    issues = []
    return a7.build_ticker_data("TEST", bars, warmup_start, issues)


def _flat(c, d=0.02, v=1000.0):
    return (c, c + d, c - d, c, v)


def _range_bar(o, c, span, v=1000.0):
    return (o, c + span, c - span, c, v)


def _rising_closes():
    return [100.0 + i for i in range(12)]


def _falling_closes():
    return [111.0 - i for i in range(12)]


def _flat_closes():
    return [100.0] * 12


def _full_day(window):
    def pf(mod, i):
        if 600 <= mod < 720:
            return window[(mod - 600) // 10]
        return 100.0
    return _bars_day(D, pf)


def _full_tdata(window):
    return a7.build_ticker_data("TEST", list(_warmup()) + _full_day(window), W0, [])


def _partial_tdata(window, last_mod=740):
    bars = list(_warmup())

    def pf(mod, i):
        if 600 <= mod < 720:
            return window[(mod - 600) // 10]
        return 100.0
    tick = a7.label_ts(D, "10:00")
    end = a7.label_ts(D, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod > last_mod:
            break
        p = pf(mod, i)
        bars.append(a7.MinuteBar(tick, p, p + 0.01, p - 0.01, p, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    return a7.build_ticker_data("TEST", bars, W0, [])


# ----------------------------------------------------------------- hashes

def test_spec_hashes_frozen_and_pinned():
    for cid in g.CANDIDATE_IDS:
        assert g.SPECS[cid]["candidate_id"] == cid
        assert g.SPECS[cid]["decision_label"] == "11:50"
        assert g.SPECS[cid]["entry_label"] == "12:00"
        assert g.SPECS[cid]["atr_excludes_deciding_bar"] is True
        assert g.SPECS[cid]["sl_atr_mult"] == 2.0
        assert g.SPECS[cid]["tp_atr_mult"] == 3.0
        recomputed = g._hash({k: v for k, v in g.SPECS[cid].items()
                              if k != "spec_hash"})
        assert recomputed == g.SPEC_HASHES[cid]


# ----------------------------------------------------------------- B06/B07

def test_B06_rising_is_long_falling_is_short_equal_no_signal():
    t_r = _tdata([_flat(c) for c in _rising_closes()])
    ev = g.evaluate("B06", t_r, D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    t_f = _tdata([_flat(c) for c in _falling_closes()])
    ev = g.evaluate("B06", t_f, D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    t_eq = _tdata([_flat(c) for c in _flat_closes()])
    ev = g.evaluate("B06", t_eq, D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


def test_B07_is_inverse_of_B06():
    t_r = _tdata([_flat(c) for c in _rising_closes()])
    ev = g.evaluate("B07", t_r, D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    t_f = _tdata([_flat(c) for c in _falling_closes()])
    ev = g.evaluate("B07", t_f, D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    t_eq = _tdata([_flat(c) for c in _flat_closes()])
    ev = g.evaluate("B07", t_eq, D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B08

def test_B08_ema_up_cross_long_down_cross_short_no_cross_skip():
    # downtrend then sharp final rise -> EMA3 crosses EMA6 up at last bar
    up = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0,
          102.0, 101.0, 100.0, 130.0]
    ev = g.evaluate("B08", _tdata([_flat(c) for c in up]), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    # uptrend then sharp final drop -> EMA3 crosses EMA6 down at last bar
    down = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0,
            108.0, 109.0, 110.0, 80.0]
    ev = g.evaluate("B08", _tdata([_flat(c) for c in down]), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    flat = _flat_closes()
    ev = g.evaluate("B08", _tdata([_flat(c) for c in flat]), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B09

def test_B09_oversold_long_overbought_short_mid_no_signal():
    oversold = [100.0] * 6 + [99.0, 98.0, 97.0, 96.0, 95.0, 94.0]
    ev = g.evaluate("B09", _tdata([_flat(c) for c in oversold]), D)
    assert ev["type"] == "signal" and ev["side"] == "long"
    assert ev["gates"]["rsi"] < 30.0 + b15.FLOAT_EPS

    overbought = [100.0] * 6 + [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    ev = g.evaluate("B09", _tdata([_flat(c) for c in overbought]), D)
    assert ev["type"] == "signal" and ev["side"] == "short"
    assert ev["gates"]["rsi"] > 70.0 - b15.FLOAT_EPS

    mid = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
           100.0, 101.0, 100.0, 101.0, 100.0, 101.0]
    ev = g.evaluate("B09", _tdata([_flat(c) for c in mid]), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B10

def test_B10_expansion_long_short_no_condition_no_signal_equal():
    long_w = [_flat(100.0)] * 11 + [_range_bar(100.0, 101.0, 0.5)]
    ev = g.evaluate("B10", _tdata(long_w), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    short_w = [_flat(100.0)] * 11 + [_range_bar(100.0, 99.0, 0.5)]
    ev = g.evaluate("B10", _tdata(short_w), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    no_cond = [_flat(100.0)] * 12
    ev = g.evaluate("B10", _tdata(no_cond), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"

    equal_w = [_flat(100.0)] * 11 + [_range_bar(100.0, 100.0, 0.5)]
    ev = g.evaluate("B10", _tdata(equal_w), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- context

def test_missing_window_bar_is_insufficient_not_no_signal():
    tdata = _tdata([_flat(c) for c in _rising_closes()], skip_window_mod=650)
    ev = g.evaluate("B06", tdata, D)
    assert ev["type"] == "insufficient_context"
    assert ev["reason"] == "incomplete_window"


def test_weekend_is_skip():
    tdata = _tdata([_flat(c) for c in _rising_closes()], decision_day=SAT)
    ev = g.evaluate("B06", tdata, SAT)
    assert ev["type"] == "skip" and ev["reason"] == "weekend_excluded"


def test_atr_warmup_insufficient_when_no_prior_bars():
    tdata = _tdata([_flat(c) for c in _rising_closes()], warmup_start=D)
    ev = g.evaluate("B06", tdata, D)
    assert ev["type"] == "insufficient_context"
    assert ev["reason"] == "atr_warmup_insufficient"


def test_entry_is_exact_12_00_and_decision_11_50():
    tdata = _tdata([_flat(c) for c in _rising_closes()])
    ev = g.evaluate("B06", tdata, D)
    assert ev["decision_label"] == "11:50"
    ie = a7.label_ts(D, "12:00").isoformat()
    assert ev["intended_entry_time"] == ie
    assert ev["decision_close_time"] == ie


def test_causality_future_bars_do_not_change_signal():
    base = [c for c in _rising_closes()]
    t_nofuture = _tdata([_flat(c) for c in base])
    ev0 = g.evaluate("B06", t_nofuture, D)
    # decision day with very different prices after 12:00
    def pf(mod, i):
        if 600 <= mod < 720:
            return base[(mod - 600) // 10]
        return 250.0 if mod % 2 == 0 else 10.0
    bars = list(_warmup()) + _bars_day(D, pf)
    t_future = a7.build_ticker_data("TEST", bars, W0, [])
    ev1 = g.evaluate("B06", t_future, D)
    assert ev1["type"] == ev0["type"]
    assert ev1.get("side") == ev0.get("side")


def test_evaluate_repeatable_same_interpreter():
    tdata = _tdata([_flat(c) for c in _rising_closes()])
    a = g.evaluate("B06", tdata, D)
    b = g.evaluate("B06", tdata, D)
    assert a == b


# ----------------------------------------------------------------- chain

def test_full_chain_signal_to_a24_filled_with_net_identity():
    a24.register_specs(g.SPEC_HASHES)
    tdata = _full_tdata([c for c in _rising_closes()])
    ev = g.evaluate("B06", tdata, D)
    assert ev["type"] == "signal"
    rec = a24.execute_candidate_signal(ev, tdata, COSTS,
                                      expected_spec_hash=g.SPEC_HASHES["B06"])
    assert rec["status"] == "FILLED"
    assert rec["net"] != "" and rec["net"] != "None"
    g_ = float(rec["gross"]); c_ = float(rec["costs_total"]); n_ = float(rec["net"])
    assert abs(n_ - (g_ - c_)) < 1e-6


def test_incomplete_has_no_numeric_net():
    a24.register_specs(g.SPEC_HASHES)
    tdata = _partial_tdata([c for c in _rising_closes()])  # entry exists, gap after
    ev = g.evaluate("B06", tdata, D)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS,
                                      expected_spec_hash=g.SPEC_HASHES["B06"])
    assert rec["status"] == "INCOMPLETE"
    assert rec["net"] in ("", None)


def test_registration_keeps_c1_and_adds_b06_b10():
    a24.register_specs(g.SPEC_HASHES)
    assert a24.CANDIDATE_SPEC_HASHES["C1"] == a24.CANDIDATE_SPEC_HASH
    assert a24.CANDIDATE_SPEC_HASHES["B06"] == g.SPEC_HASHES["B06"]
    assert a24.CANDIDATE_SPEC_HASHES["B10"] == g.SPEC_HASHES["B10"]
    assert "B06" in a24.CANDIDATES and "C3" in a24.CANDIDATES
