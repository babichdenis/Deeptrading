"""BATCH-15 group A (B01–B05): постоянные synthetic-тесты (B1).

Герметично: синтетика в памяти, offline. Направления каждого кандидата, неполное
окно -> insufficient_context (НЕ no_signal), weekend -> skip, ATR warmup/недостаток,
равенство/порог -> no_signal, причинность (будущие бары не влияют), точная минута
входа 11:00, решающий бар 10:50, полная цепочка signal->A24->ledger, регистрация
spec в A24 без переименования C1, повторяемость evaluate.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_strategies_core as a7
import batch15_group_a as g
import batch15_common as b15
import day_candidate_execution as a24

W0 = date(2026, 6, 8)
W1 = date(2026, 6, 9)
W2 = date(2026, 6, 10)
D = date(2026, 6, 11)            # Thu
SAT = date(2026, 6, 13)          # Sat
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)


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
    return a7.build_ticker_data("TEST", bars, warmup_start, [])


def _partial_tdata(window, last_mod=690):
    bars = list(_warmup())
    for k, (o, h, l, c, v) in enumerate(window):
        base_mod = 600 + k * 10
        mid = (o + c) / 2.0
        for j in range(10):
            mod = base_mod + j
            ts = a7.label_ts(D, f"{mod // 60:02d}:{mod % 60:02d}")
            if j == 0:
                bars.append(a7.MinuteBar(ts, o, h, l, o, v / 10.0))
            elif j == 9:
                bars.append(a7.MinuteBar(ts, mid, h, l, c, v / 10.0))
            else:
                bars.append(a7.MinuteBar(ts, mid, h, l, mid, v / 10.0))
    # минуты после окна до last_mod (flat, без SL/TP): вход известен, выхода нет
    p = 100.0
    for mod in range(660, last_mod + 1):
        ts = a7.label_ts(D, f"{mod // 60:02d}:{mod % 60:02d}")
        bars.append(a7.MinuteBar(ts, p, p + 0.01, p - 0.01, p, 10.0))
    return a7.build_ticker_data("TEST", bars, W0, [])


def _flat(c, d=0.02, v=1000.0):
    return (c, c + d, c - d, c, v)


def _ohlc(o, h, l, c, v=1000.0):
    return (o, h, l, c, v)


# ----------------------------------------------------------------- hashes

def test_spec_hashes_frozen_and_pinned():
    for cid in g.CANDIDATE_IDS:
        assert g.SPECS[cid]["candidate_id"] == cid
        assert g.SPECS[cid]["decision_label"] == "10:50"
        assert g.SPECS[cid]["entry_label"] == "11:00"
        assert g.SPECS[cid]["atr_excludes_deciding_bar"] is True
        assert g.SPECS[cid]["sl_atr_mult"] == 2.0
        assert g.SPECS[cid]["tp_atr_mult"] == 3.0
        recomputed = g._hash({k: v for k, v in g.SPECS[cid].items()
                              if k != "spec_hash"})
        assert recomputed == g.SPEC_HASHES[cid]


# ----------------------------------------------------------------- B01/B02

def test_B01_rising_is_long_falling_is_short_equal_no_signal():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    ev = g.evaluate("B01", _tdata(up), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    down = [_flat(110.0)] * 5 + [_flat(100.0)]
    ev = g.evaluate("B01", _tdata(down), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    flat = [_flat(100.0)] * 6
    ev = g.evaluate("B01", _tdata(flat), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


def test_B02_is_inverse_of_B01():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    ev = g.evaluate("B02", _tdata(up), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    down = [_flat(110.0)] * 5 + [_flat(100.0)]
    ev = g.evaluate("B02", _tdata(down), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    flat = [_flat(100.0)] * 6
    ev = g.evaluate("B02", _tdata(flat), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B03

def test_B03_breakout_long_short_no_condition():
    # OR H=100.5 (bars10:00..10:20 close 100), C11=102 breakout up
    up = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 3 + [_flat(100.0)] * 2 + [_flat(102.0)]
    ev = g.evaluate("B03", _tdata(up), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    down = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 3 + [_flat(100.0)] * 2 + [_flat(98.0)]
    ev = g.evaluate("B03", _tdata(down), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    inside = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 3 + [_flat(100.0)] * 3
    ev = g.evaluate("B03", _tdata(inside), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B04

def test_B04_up_probe_revert_short_down_probe_revert_long():
    # пробой вверх в10:30..10:49 (high>100.5), C11 обратно внутри [99.5,100.5] -> short
    up_probe = [_ohlc(100.0, 100.5, 99.5, 100.0), _ohlc(100.0, 100.5, 99.5, 100.0),
                _ohlc(100.0, 100.5, 99.5, 100.0),
                _ohlc(100.0, 101.5, 99.5, 100.5), _ohlc(100.5, 100.5, 99.5, 100.0),
                _flat(100.0)]
    ev = g.evaluate("B04", _tdata(up_probe), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    # пробой вниз (low<99.5), C11 внутри -> long
    down_probe = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 3 + \
        [_ohlc(100.0, 100.5, 98.5, 99.5), _ohlc(100.0, 100.5, 99.5, 100.0),
         _flat(100.0)]
    ev = g.evaluate("B04", _tdata(down_probe), D)
    assert ev["type"] == "signal" and ev["side"] == "long"


def test_B04_no_break_or_ambiguous_no_signal():
    no_break = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 6
    ev = g.evaluate("B04", _tdata(no_break), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"

    # оба пробоя -> ambiguous
    both = [_ohlc(100.0, 100.5, 99.5, 100.0)] * 3 + \
        [_ohlc(100.0, 101.5, 99.5, 100.5), _ohlc(100.0, 100.5, 98.5, 99.5),
         _flat(100.0)]
    ev = g.evaluate("B04", _tdata(both), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- B05

def test_B05_location_long_short_middle():
    # окно10:00..10:50, H=102,L=99 (через high/low баров); C11 ближе к верху
    hi = [_ohlc(100.0, 102.0, 99.0, 99.5)] * 5 + [_flat(101.9)]
    ev = g.evaluate("B05", _tdata(hi), D)
    assert ev["type"] == "signal" and ev["side"] == "long"

    lo = [_ohlc(100.0, 102.0, 99.0, 99.5)] * 5 + [_flat(99.1)]
    ev = g.evaluate("B05", _tdata(lo), D)
    assert ev["type"] == "signal" and ev["side"] == "short"

    mid = [_ohlc(100.0, 102.0, 99.0, 99.5)] * 5 + [_flat(100.5)]
    ev = g.evaluate("B05", _tdata(mid), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


def test_B05_flat_range_no_signal():
    flat = [_flat(100.0)] * 6
    ev = g.evaluate("B05", _tdata(flat), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ----------------------------------------------------------------- context

def test_missing_window_bar_is_insufficient_not_no_signal():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up, skip_window_mod=640)
    for cid in ("B01", "B02", "B03", "B04", "B05"):
        ev = g.evaluate(cid, tdata, D)
        assert ev["type"] == "insufficient_context", cid
        assert ev["reason"] == "incomplete_window", cid


def test_weekend_is_skip():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up, decision_day=SAT)
    ev = g.evaluate("B01", tdata, SAT)
    assert ev["type"] == "skip" and ev["reason"] == "weekend_excluded"


def test_atr_warmup_insufficient_when_no_prior_bars():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up, warmup_start=D)
    ev = g.evaluate("B01", tdata, D)
    assert ev["type"] == "insufficient_context"
    assert ev["reason"] == "atr_warmup_insufficient"


def test_entry_is_exact_11_00_and_decision_10_50():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up)
    ev = g.evaluate("B01", tdata, D)
    assert ev["decision_label"] == "10:50"
    ie = a7.label_ts(D, "11:00").isoformat()
    assert ev["intended_entry_time"] == ie
    assert ev["decision_close_time"] == ie


def test_causality_future_bars_do_not_change_signal():
    base = [_flat(100.0)] * 5 + [_flat(110.0)]
    closes = [t[0] for t in base]
    t_nofuture = _tdata(base)
    ev0 = g.evaluate("B01", t_nofuture, D)

    def pf(mod, i):
        if 600 <= mod < 660:
            return closes[(mod - 600) // 10]
        return 250.0 if mod % 2 == 0 else 10.0
    bars = list(_warmup()) + _bars_day(D, pf)
    t_future = a7.build_ticker_data("TEST", bars, W0, [])
    ev1 = g.evaluate("B01", t_future, D)
    assert ev1["type"] == ev0["type"]
    assert ev1.get("side") == ev0.get("side")


def test_evaluate_repeatable_same_interpreter():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up)
    assert g.evaluate("B01", tdata, D) == g.evaluate("B01", tdata, D)


def test_unknown_candidate_raises():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _tdata(up)
    with pytest.raises(ValueError):
        g.evaluate("B99", tdata, D)


# ----------------------------------------------------------------- chain

def test_full_chain_signal_to_a24_filled_with_net_identity():
    # полный день с трендом, чтобы _walk нашёл выход (SL/TP)
    def pf(mod, i):
        if 600 <= mod < 720:
            return 100.0 + (mod - 600) * 0.05
        return 100.0 + max(0, mod - 720) * 0.2
    bars = list(_warmup()) + _bars_day(D, pf)
    tdata = a7.build_ticker_data("TEST", bars, W0, [])
    ev = g.evaluate("B01", tdata, D)
    assert ev["type"] == "signal"
    a24.register_specs(g.SPEC_HASHES)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS,
                                      expected_spec_hash=g.SPEC_HASHES["B01"])
    assert rec["status"] == "FILLED"
    g_ = float(rec["gross"]); c_ = float(rec["costs_total"]); n_ = float(rec["net"])
    assert abs(n_ - (g_ - c_)) < 1e-6


def test_incomplete_has_no_numeric_net():
    up = [_flat(100.0)] * 5 + [_flat(110.0)]
    tdata = _partial_tdata(up)  # entry bar есть, путь после входа отсутствует
    ev = g.evaluate("B01", tdata, D)
    a24.register_specs(g.SPEC_HASHES)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS,
                                      expected_spec_hash=g.SPEC_HASHES["B01"])
    assert rec["status"] == "INCOMPLETE"
    assert rec["net"] in ("", None)


def test_registration_keeps_c1_and_adds_b01_b05():
    a24.register_specs(g.SPEC_HASHES)
    assert a24.CANDIDATE_SPEC_HASHES["C1"] == a24.CANDIDATE_SPEC_HASH
    assert a24.CANDIDATE_SPEC_HASHES["B01"] == g.SPEC_HASHES["B01"]
    assert a24.CANDIDATE_SPEC_HASHES["B05"] == g.SPEC_HASHES["B05"]
    assert "B01" in a24.CANDIDATES and "C3" in a24.CANDIDATES
