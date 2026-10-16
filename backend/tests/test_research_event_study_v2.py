"""RESEARCH-CORE-V2 event study (B2): focused synthetic-тесты.

Герметично, offline, без DB/broker: endpoint missing -> UNKNOWN, timestamp
causality, costs/accounting через общий research_execution, intermediate gap ->
path metrics UNKNOWN (endpoint остаётся), дневной режим на prev-day, ER60,
arms A/B/C, direction controls.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_strategies_core as a7
import research_event_study_v2 as es

W0 = date(2026, 6, 8)
W1 = date(2026, 6, 9)
W2 = date(2026, 6, 10)
D = date(2026, 6, 11)
COSTS = list(es.COST_SCENARIOS)


def _bars_day(day, open_fn, skip_mods=(), price_hi=0.05):
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod not in skip_mods:
            p = open_fn(mod)
            bars.append(a7.MinuteBar(tick, p, p + price_hi, max(p - price_hi, 0.01),
                                     p, 10.0))
        tick += timedelta(minutes=1)
    return bars


def _flat(day):
    return _bars_day(day, lambda m: 100.0)


def _tdata_pre_signal():
    """Warmup + day D: 10:00 open=100; 10:50 close=101 (momentum up); 11:00=101."""
    def px(m):
        if m == 600:                      # 10:00 bar open
            return 100.0
        if 600 < m < 650:
            return 100.5
        if 650 <= m < 660:                # 10:50 bar close -> 101
            return 101.0
        if m == 660:                      # 11:00 entry open
            return 101.0
        if m == 690:                      # 11:30 exit
            return 102.0
        return 101.0
    bars = _flat(W0) + _flat(W1) + _flat(W2)
    bars += _bars_day(D, px)
    issues = []
    return a7.build_ticker_data("TEST", bars, W0, issues)


# ------------------------------------------------------------------ endpoint

def test_horizon_unknown_when_exit_minute_missing():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    missing_exit = a7.label_ts(D, "12:00")
    # remove the exit minute entirely
    td.minute_by_ts.pop(missing_exit)
    r = es.horizon_outcome(td, D, entry, 1, missing_exit, COSTS)
    assert r["status"] == "unknown"
    assert r["reason"] == "missing_exit_minute"


def test_horizon_unknown_when_entry_missing():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    td.minute_by_ts.pop(entry)
    r = es.horizon_outcome(td, D, entry, 1, a7.label_ts(D, "11:30"), COSTS)
    assert r["status"] == "unknown"
    assert r["reason"] == "missing_entry_minute"


def test_horizon_out_of_session_is_unknown():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    out = a7.label_ts(D, "19:30")   # beyond session
    r = es.horizon_outcome(td, D, entry, 1, out, COSTS)
    assert r["status"] == "unknown"
    assert r["reason"] == "exit_out_of_session"


# ------------------------------------------------------------------ accounting

def test_costs_matched_to_shared_research_execution():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    exit_ts = a7.label_ts(D, "11:30")
    r = es.horizon_outcome(td, D, entry, 1, exit_ts, COSTS)
    assert r["status"] == "ok"
    assert r["gross"] == pytest.approx(102.0 / 101.0 - 1.0, rel=0, abs=1e-15)
    assert r["break_even"] == r["gross"]
    for cid in ("c5_s2", "c10_s5"):
        net = r["net"][cid]
        drag = r["costs"][cid]
        assert net == pytest.approx(r["gross"] - drag, rel=0, abs=1e-12)
    # dearer cost has larger drag and smaller net
    assert r["costs"]["c10_s5"] > r["costs"]["c5_s2"]
    assert r["net"]["c10_s5"] < r["net"]["c5_s2"]


def test_short_side_sign_is_negative_of_long():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    exit_ts = a7.label_ts(D, "11:30")
    long_r = es.horizon_outcome(td, D, entry, 1, exit_ts, COSTS)
    short_r = es.horizon_outcome(td, D, entry, -1, exit_ts, COSTS)
    assert short_r["gross"] == pytest.approx(-long_r["gross"], abs=1e-15)


# ------------------------------------------------------------------ path gap

def test_intermediate_gap_keeps_endpoint_but_path_incomplete():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    exit_ts = a7.label_ts(D, "12:00")
    td.minute_by_ts.pop(a7.label_ts(D, "11:30"))   # hole strictly between
    r = es.horizon_outcome(td, D, entry, 1, exit_ts, COSTS)
    assert r["status"] == "ok"            # endpoint still known
    assert r["path_complete"] is False
    assert r["path_gap_ts"] != ""


# ------------------------------------------------------------------ causality

def test_entry_exit_exact_minutes_and_causality():
    td = _tdata_pre_signal()
    entry = a7.label_ts(D, "11:00")
    r = es.horizon_outcome(td, D, entry, 1, a7.label_ts(D, "11:30"), COSTS)
    assert r["entry_base"] == 101.0       # open of 11:00
    assert r["exit_base"] == 102.0        # open of 11:30
    # adding future bars after the endpoint must not change the outcome
    for i in range(1, 20):
        t = a7.label_ts(D, "11:31") + timedelta(minutes=i)
        td.minute_by_ts[t] = a7.MinuteBar(t, 999.0, 999.0, 999.0, 999.0, 1.0)
    r2 = es.horizon_outcome(td, D, entry, 1, a7.label_ts(D, "11:30"), COSTS)
    assert r2["gross"] == r["gross"]


# ------------------------------------------------------------------ daily regime

def test_daily_features_prev_day_only_and_er_formula():
    series = []
    d = date(2026, 1, 1)
    price = 100.0
    for i in range(80):
        series.append((d, price, True))
        price *= 1.01
        d += timedelta(days=1)
    feat, days = es._daily_features_for(series)
    last = feat[days[-1]]
    assert last["ret20"] == pytest.approx(series[-1][1] / series[-21][1] - 1.0)
    assert last["er60"] is not None and 0.0 <= last["er60"] <= 1.0
    # ER60 == |C_t - C_{t-60}| / sum|dC|; строго монотонный ряд -> ER60 == 1
    assert last["er60"] == pytest.approx(1.0, rel=1e-9)


def test_er60_zero_denominator_is_zero():
    series = [(date(2026, 1, 1) + timedelta(days=i), 100.0, True) for i in range(70)]
    feat, days = es._daily_features_for(series)
    assert feat[days[-1]]["er60"] == 0.0


def test_regime_uses_previous_completed_day():
    series = []
    d = date(2026, 1, 1)
    price = 100.0
    for i in range(80):
        series.append((d, price, True))
        price *= 1.005
        d += timedelta(days=1)
    feat, days = es._daily_features_for(series)
    # first eval day far enough to have sma60/ret60 -> UP
    for probe in days:
        if feat[probe]["sma60"] is not None and feat[probe]["ret60"] is not None:
            break
    target = probe + timedelta(days=1)
    regime, detail = es.regime_for(days, feat, target)
    assert regime == "UP"
    # very early day: insufficient history -> UNKNOWN
    regime0, detail0 = es.regime_for(days, feat, days[1])
    assert regime0 is None
    assert "insufficient" in detail0


# ------------------------------------------------------------------ arms

def _sig(day, ticker, side, regime, er60):
    return {"type": "signal", "candidate_id": "B01", "ticker": ticker,
            "day": day, "side": side, "regime": regime, "er60": er60,
            "horizons": {}, "controls": {}}


def test_arms_assignment_b_and_c():
    evs = [
        _sig("2026-06-11", "AAA", "long", "UP", 0.5),
        _sig("2026-06-11", "BBB", "long", "MIXED", 0.9),
        _sig("2026-06-11", "CCC", "short", "DOWN", 0.7),
        _sig("2026-06-11", "DDD", "long", "UP", 0.2),
        _sig("2026-06-12", "AAA", "long", "UNKNOWN", 0.3),
    ]
    es.assign_arms(evs, top_n=2)
    by = {e["ticker"] + e["day"][-2:]: e for e in evs}
    assert all(e["arm_a"] for e in evs)                       # A = all signals
    assert by["AAA11"]["arm_b"] and by["CCC11"]["arm_b"] and by["DDD11"]["arm_b"]
    assert not by["BBB11"]["arm_b"] and by["BBB11"]["arm_reason"] == "no_trade_mixed"
    assert not by["AAA12"]["arm_b"] and by["AAA12"]["arm_reason"] == "unknown_filter"
    # C: per day top_n by er60 among arm_b
    assert by["AAA11"]["arm_c"] and by["CCC11"]["arm_c"]
    assert not by["DDD11"]["arm_c"]                            # er60 0.2 lowest
    assert not by["BBB11"]["arm_c"]                            # not arm_b


# ------------------------------------------------------------------ end to end

def test_evaluate_ticker_signal_and_event_id():
    td = _tdata_pre_signal()
    by_cid, spec_hashes = es._load_groups()
    series = es.daily_series(td)
    feat_by_day, feat_days = es._daily_features_for(series)
    events = es.evaluate_ticker("TEST", td, [D], COSTS, spec_hashes, "deadbeef",
                                by_cid, feat_days, feat_by_day)
    by_cid_day = {(e["candidate_id"], e["day"]): e for e in events}
    e = by_cid_day[("B01", D.isoformat())]
    assert e["event_id"] == f"B01|TEST|{D.isoformat()}"
    assert e["type"] == "signal" and e["side"] == "long"
    assert e["horizons"]["h30"]["status"] == "ok"
    assert e["horizons"]["h30"]["gross"] == pytest.approx(102.0 / 101.0 - 1.0)
    # regime: only 4 synthetic sessions -> insufficient history -> UNKNOWN
    assert e["regime"] == "UNKNOWN"
    # every candidate is present for the day (skip/insufficient/signal)
    assert len({x["candidate_id"] for x in events}) == 15
