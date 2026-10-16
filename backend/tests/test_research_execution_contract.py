"""B2 EXECUTION CONTRACT, step 1: pin current A7/A24 behavior before refactor.

Hermetic: synthetic minutesCSVs in tmp_path only; no network, no DB, no app.*.
Float asserts use ABS_TOL=1e-9 (explicit, justified: double arithmetic over small
products; economic thresholds keep their own FLOAT_EPS semantics from the core).
Codes/reasons/counts assert exact equality.

Sections P* pin behavior that must stay green through the step-2 refactor
(shared research_execution core, REPLAY contract fixes). Sections F* are
failing-first: they fail until the step-2 fix lands (missing module, duplicate
copies, silent REPLAY continue).
"""

from __future__ import annotations

import csv
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_candidate_execution as a24
import day_strategies_core as a7

ABS_TOL = 1e-9
TICKER = "TST"
D0 = date(2026, 6, 15)  # Monday
D1 = date(2026, 6, 16)
D2 = date(2026, 6, 17)
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)
ZERO_COSTS = a7.CostParams(commission_bps=0.0, slippage_bps=0.0)


def drift_bars(day, start=100.0, slope=0.01, volume=10.0, skip_mods=()):
    """Full-session synthetic minutes; o==c linear drift, tight valid range."""
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    i = 0
    while tick <= end:
        if a7.msk_mod(tick) not in skip_mods:
            close = start + slope * i
            bars.append(a7.MinuteBar(tick, close, close + 0.005, close - 0.005,
                                     close, volume))
        tick += timedelta(minutes=1)
        i += 1
    return bars


def flat_bars(day, price=100.0, volume=10.0, skip_mods=()):
    return drift_bars(day, start=price, slope=0.0, volume=volume,
                      skip_mods=skip_mods)


def make_tdata(days, warmup_start=None, issues=None):
    bars = []
    for day, kind, kwargs in days:
        bars.extend(kind(day, **kwargs))
    if issues is None:
        issues = []
    return a7.build_ticker_data(TICKER, bars,
                                warmup_start or days[0][0], issues)


def exec_signal(day, entry_msk="12:10", side="long", atr=0.5, cid="C1"):
    ie = a7.label_ts(day, entry_msk).isoformat()
    return {
        "type": "signal", "candidate_id": cid, "ticker": TICKER,
        "day": day.isoformat(), "spec_hash": a24.CANDIDATE_SPEC_HASH,
        "side": side, "atr": atr, "decision_label": "12:00",
        "decision_close_time": ie, "intended_entry_time": ie,
        "observed_until": ie,
    }


def write_csv(path: Path, rows: list) -> str:
    import hashlib
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(a7.REQUIRED_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def minute_row(ticker, tick, price, volume=10.0):
    return {"ticker": ticker, "ts": tick.isoformat(), "open": price,
            "high": price + 0.005, "low": price - 0.005, "close": price,
            "volume": volume}


def c1_breakout_days():
    """D0 flat warmup; D1 flat OR 10:00-10:20 then 10:30 breakout close."""
    d0 = drift_bars(D0, start=100.0, slope=0.0)
    d1 = []
    tick = a7.label_ts(D1, "10:00")
    end = a7.label_ts(D1, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod < 630:
            price = 100.0
        elif mod < 640:
            price = 100.0 + 0.05 * (mod - 629)
        else:
            price = 100.5
        d1.append(a7.MinuteBar(tick, price, price + 0.005, price - 0.005,
                               price, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    return [(D0, lambda day, **k: d0), (D1, lambda day, **k: d1)]


# ---------------------------------------------------------------- P: loader/DQ

def test_p_load_identical_duplicates_collapse_to_one_with_notice(tmp_path):
    rows = [minute_row(TICKER, a7.label_ts(D0, "10:00"), 100.0),
            minute_row(TICKER, a7.label_ts(D0, "10:00"), 100.0),
            minute_row(TICKER, a7.label_ts(D0, "10:01"), 100.0)]
    path = tmp_path / "m.csv"
    write_csv(path, rows)
    by_ticker, issues, _sha = a7.load_minute_csv(path)
    assert len(by_ticker[TICKER]) == 2
    assert any(i.code == "duplicate_identical" and i.kind == "notice"
               for i in issues)


def test_p_load_conflicting_duplicates_drop_both_with_error(tmp_path):
    rows = [minute_row(TICKER, a7.label_ts(D0, "10:00"), 100.0),
            minute_row(TICKER, a7.label_ts(D0, "10:00"), 101.0)]
    path = tmp_path / "m.csv"
    write_csv(path, rows)
    by_ticker, issues, _sha = a7.load_minute_csv(path)
    assert by_ticker.get(TICKER, []) == []
    assert any(i.code == "duplicate_conflicting" and i.kind == "error"
               for i in issues)


def test_p_load_minute_not_aligned_is_error(tmp_path):
    tick = a7.label_ts(D0, "10:00") + timedelta(seconds=30)
    path = tmp_path / "m.csv"
    write_csv(path, [minute_row(TICKER, tick, 100.0)])
    by_ticker, issues, _sha = a7.load_minute_csv(path)
    assert by_ticker.get(TICKER, []) == []
    assert any(i.code == "minute_not_aligned" for i in issues)


def test_p_load_broken_ohlc_marks_minute_corrupted(tmp_path):
    tick = a7.label_ts(D0, "10:00")
    good = minute_row(TICKER, tick, 100.0)
    bad = dict(good)
    bad["high"] = 99.0  # high < open -> ohlc_broken
    path = tmp_path / "m.csv"
    write_csv(path, [good, bad])
    by_ticker, issues, _sha = a7.load_minute_csv(path)
    assert by_ticker.get(TICKER, []) == []
    codes = {i.code for i in issues}
    assert "ohlc_broken" in codes
    assert "minute_corrupted" in codes


def test_p_full_bar_requires_exactly_10_distinct_minutes():
    tdata = make_tdata([(D0, flat_bars, {})])
    label = a7.label_ts(D0, "10:00")
    assert tdata.buckets[label].is_full is True
    short = a7.Bar10(label, [a7.MinuteBar(label, 100, 100, 100, 100, 1.0)],
                     100, 100, 100, 100, 1.0)
    assert short.is_full is False


# ---------------------------------------------------------------- P: S1 gates

def test_p_s1_long_fills_on_time_exit():
    tdata = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                        (D2, drift_bars, {})])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert rec["status"] == "FILLED"
    assert rec["exit_reason"] == "time_exit_18_30"
    assert rec["side"] == "long"
    assert math.isclose(rec["net"], rec["gross"] - rec["costs_total"],
                        abs_tol=ABS_TOL)


def test_p_s1_short_fills_on_time_exit():
    tdata = make_tdata([(D0, drift_bars, {"slope": -0.01}),
                        (D1, drift_bars, {"slope": -0.01}),
                        (D2, drift_bars, {"slope": -0.01})])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert rec["status"] == "FILLED"
    assert rec["side"] == "short"


def test_p_s1_no_signal_below_theta():
    tdata = make_tdata([(D0, flat_bars, {}), (D1, flat_bars, {}),
                        (D2, flat_bars, {})])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert "status" not in rec
    assert rec["reason"] == "no_signal_theta"


def test_p_s1_entry_minute_missing_is_skip_not_search():
    tdata = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                        (D2, drift_bars, {"skip_mods": (730,)})])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert "status" not in rec
    assert rec["reason"] == "entry_minute_missing"


# ---------------------------------------------------------------- P: S2 gates

def test_p_s2_fade_short_fills():
    d1 = flat_bars(D1, price=100.0)
    d2 = []
    tick = a7.label_ts(D2, "10:00")
    end = a7.label_ts(D2, "18:44")
    while tick <= end:
        d2.append(a7.MinuteBar(tick, 101.0, 101.005, 100.995, 101.0, 10.0))
        tick += timedelta(minutes=1)
    issues = []
    tdata = a7.build_ticker_data(
        TICKER, drift_bars(D0, slope=0.005) + d1 + d2, D0, issues)
    rec = a7.run_day(tdata, a7.CONFIGS[2], D2, COSTS)
    assert rec["status"] == "FILLED"
    assert rec["side"] == "short"


def test_p_s2_no_gap_is_skip():
    d1 = flat_bars(D1, price=100.0)
    d2 = flat_bars(D2, price=100.5)  # gap 0.005 < gmin 0.007
    tdata = a7.build_ticker_data(TICKER, drift_bars(D0, slope=0.005) + d1 + d2,
                                 D0, [])
    rec = a7.run_day(tdata, a7.CONFIGS[2], D2, COSTS)
    assert rec.get("reason") == "no_gap"


def test_p_s2_no_continuation_is_skip():
    d1 = flat_bars(D1, price=100.0)
    d2 = []
    tick = a7.label_ts(D2, "10:00")
    end = a7.label_ts(D2, "18:44")
    i = 0
    while tick <= end:  # trends WITH the gap -> no fade
        price = 101.0 + 0.01 * i
        d2.append(a7.MinuteBar(tick, price, price + 0.005, price - 0.005,
                               price, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    tdata = a7.build_ticker_data(TICKER, drift_bars(D0, slope=0.005) + d1 + d2,
                                 D0, [])
    rec = a7.run_day(tdata, a7.CONFIGS[2], D2, COSTS)
    assert rec.get("reason") == "no_signal_continuation"


# ---------------------------------------------------------------- P: ATR gates

def test_p_atr_warmup_insufficient():
    tdata = make_tdata([(D2, drift_bars, {})])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert rec.get("reason") == "atr_warmup_insufficient"


def test_p_atr_invalid_on_flat_zero_range():
    bars = []
    for day in (D0, D1, D2):
        tick = a7.label_ts(day, "10:00")
        end = a7.label_ts(day, "18:44")
        while tick <= end:  # h==l everywhere -> TR 0 -> ATR 0
            bars.append(a7.MinuteBar(tick, 100.0, 100.0, 100.0, 100.0, 10.0))
            tick += timedelta(minutes=1)
    tdata = a7.build_ticker_data(TICKER, bars, D0, [])
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert rec.get("reason") in ("no_signal_theta", "atr_invalid")


def test_p_atr_excludes_deciding_bar():
    tdata = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                        (D2, drift_bars, {})])
    dec = a7._full_bar(tdata, D2, "12:00")
    atr_plain, _n = a7.atr_before(tdata, dec.ts)
    mutated = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                          (D2, drift_bars, {})])
    key = (D2, a7.label_mod("12:00"))
    bar = mutated.bar_by_day_label[key]
    object.__setattr__(bar, "close", bar.close + 5.0)
    atr_mut, _n2 = a7.atr_before(mutated, dec.ts)
    assert atr_plain == atr_mut


# ---------------------------------------------------------------- P: _walk exits

def _walk_tdata(day_mods):
    """One decision day D2 with explicit minute overrides.

    day_mods: {msk_mod: (o,h,l,c) or None for missing} applied over drift.
    """
    bars = drift_bars(D0, slope=0.005) + drift_bars(D1, slope=0.005)
    tick = a7.label_ts(D2, "10:00")
    end = a7.label_ts(D2, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod in day_mods and day_mods[mod] is None:
            pass
        elif mod in day_mods:
            o, h, l, c = day_mods[mod]
            bars.append(a7.MinuteBar(tick, o, h, l, c, 10.0))
        else:
            c = 101.0 + 0.001 * i
            bars.append(a7.MinuteBar(tick, c, c + 0.005, c - 0.005, c, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    return a7.build_ticker_data(TICKER, bars, D0, [])


def _long_walk(tdata, entry_msk="12:10", atr=0.05):
    entry_ts = a7.label_ts(D2, entry_msk)
    base = tdata.minute_by_ts[entry_ts].open
    sl = base - 2.0 * atr
    return a7._walk(tdata, D2, entry_ts, 1, sl, None), base, sl


def test_p_walk_sl_touch_before_tp():
    day = {750: (101.2, 101.6, 100.0, 101.2)}  # open above SL, low breaks it
    tdata = _walk_tdata(day)
    (kind, base, tick, reason), _entry, sl = _long_walk(tdata)
    assert kind == "exit" and reason == "sl_touch"
    assert base == pytest.approx(sl, abs=ABS_TOL)


def test_p_walk_sl_gap_open_uses_adverse_open():
    day = {750: (99.0, 99.2, 98.8, 99.0)}  # open far below SL
    tdata = _walk_tdata(day)
    (kind, base, _tick, reason), _entry, sl = _long_walk(tdata)
    assert kind == "exit" and reason == "sl_gap_open"
    assert base == pytest.approx(99.0, abs=ABS_TOL)
    assert base < sl


def test_p_walk_tp_gap_open_executes_at_level_not_open():
    tdata = _walk_tdata({})
    entry_ts = a7.label_ts(D2, "12:10")
    base = tdata.minute_by_ts[entry_ts].open
    sl, tp = base - 0.1, base + 0.05
    day = {750: (base + 5.0, base + 5.1, base + 4.9, base + 5.0)}
    tdata = _walk_tdata(day)
    kind, out_base, _tick, reason = a7._walk(tdata, D2, entry_ts, 1, sl, tp)
    assert kind == "exit" and reason == "tp_gap_open"
    assert out_base == pytest.approx(tp, abs=ABS_TOL)


def test_p_walk_sl_before_tp_on_same_bar():
    tdata = _walk_tdata({})
    entry_ts = a7.label_ts(D2, "12:10")
    base = tdata.minute_by_ts[entry_ts].open
    sl, tp = base - 0.1, base + 0.1
    day = {750: (base, base + 5.0, base - 5.0, base)}
    tdata = _walk_tdata(day)
    kind, _b, _t, reason = a7._walk(tdata, D2, entry_ts, 1, sl, tp)
    assert kind == "exit" and reason == "sl_touch"


def test_p_walk_time_exit_has_priority_over_sl_on_1830():
    mods = {}
    for mod in range(1100, 1110):  # full 18:20 bar, opens safely above SL
        mods[mod] = (101.2, 101.205, 101.195, 101.2)
    mods[1110] = (100.0, 101.0, 90.0, 100.5)  # 18:30 low breaks SL too
    tdata = _walk_tdata(mods)
    entry_ts = a7.label_ts(D2, "12:10")
    base = tdata.minute_by_ts[entry_ts].open
    kind, _b, _t, reason = a7._walk(tdata, D2, entry_ts, 1, base - 0.1, None)
    assert kind == "exit" and reason == "time_exit_18_30"


def test_p_walk_path_gap_after_entry():
    tdata = _walk_tdata({780: None})  # 13:00 missing
    (kind, reason, gap_ts), _e, _s = _long_walk(tdata)
    assert kind == "incomplete" and reason == "path_gap_after_entry"
    assert a7.msk_mod(gap_ts) == 780


def test_p_walk_stop_before_later_gap_stays_filled():
    """Step 3: a stop that executed before a later data gap stays FILLED and
    the gap must not reclassify it as INCOMPLETE."""
    bars = (drift_bars(D0, start=100.0, slope=0.05)
            + drift_bars(D1, start=100.0, slope=0.05))
    tick = a7.label_ts(D2, "10:00")
    end = a7.label_ts(D2, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod == 780:                 # 13:00: SL executed here
            bars.append(a7.MinuteBar(tick, 108.0, 108.1, 50.0, 108.0, 10.0))
        elif mod == 840:               # 14:00: data gap AFTER the stop
            pass
        else:
            c = 100.0 + 0.05 * i
            bars.append(a7.MinuteBar(tick, c, c + 0.005, c - 0.005, c, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    tdata = a7.build_ticker_data(TICKER, bars, D0, [])
    (kind, base, exit_ts, reason), _entry, sl = _long_walk(tdata)
    assert kind == "exit" and reason == "sl_touch"
    assert a7.msk_mod(exit_ts) == 780
    assert base == pytest.approx(sl, abs=ABS_TOL)
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert rec["status"] == "FILLED"
    assert rec["exit_reason"] == "sl_touch"
    assert math.isfinite(rec["net"])


def test_p_walk_time_exit_bar_1820_incomplete():
    mods = {m: None for m in range(1100, 1110)}  # 18:20 bar not full
    tdata = _walk_tdata(mods)
    (kind, reason, _g), _e, _s = _long_walk(tdata)
    assert kind == "incomplete" and reason == "time_exit_bar_18_20_incomplete"


def test_p_walk_time_exit_minute_1830_missing():
    mods = {1110: None}  # 18:30 minute missing, later minutes exist
    tdata = _walk_tdata(mods)
    (kind, reason, _g), _e, _s = _long_walk(tdata)
    assert kind == "incomplete" and reason == "time_exit_minute_18_30_missing"


def test_p_walk_none_means_data_ended_open_position():
    bars = drift_bars(D0, slope=0.005) + drift_bars(D1, slope=0.005)
    tick = a7.label_ts(D2, "10:00")  # day cut at 15:00, before plan window
    end = a7.label_ts(D2, "15:00")
    i = 0
    while tick <= end:
        c = 101.0 + 0.001 * i
        bars.append(a7.MinuteBar(tick, c, c + 0.005, c - 0.005, c, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    tdata = a7.build_ticker_data(TICKER, bars, D0, [])
    entry_ts = a7.label_ts(D2, "12:10")
    base = tdata.minute_by_ts[entry_ts].open
    assert a7._walk(tdata, D2, entry_ts, 1, base - 5.0, None) is None


# ---------------------------------------------------------------- P: EXEC statuses

def test_p_exec_reject_codes():
    tdata = make_tdata([(D2, flat_bars, {})])
    good = exec_signal(D2)
    cases = [
        ({"type": "skip"}, "not_a_signal"),
        ({k: v for k, v in good.items() if k != "atr"}, "missing_field"),
        ({**good, "candidate_id": "C9"}, "unknown_candidate"),
        ({**good, "ticker": "OTHER"}, "ticker_mismatch"),
        ({**good, "spec_hash": "0" * 64}, "spec_hash_mismatch"),
        ({**good, "side": "up"}, "invalid_side"),
        ({**good, "atr": 0.0}, "atr_invalid"),
        ({**good, "intended_entry_time": "not-a-ts"}, "ts_unparsable"),
        ({**good, "intended_entry_time": a7.label_ts(D2, "12:11").isoformat()},
         "entry_time_mismatch"),
        ({**good, "day": D1.isoformat()}, "day_mismatch"),
    ]
    for signal, code in cases:
        rec = a24.execute_candidate_signal(signal, tdata, COSTS)
        assert rec["status"] == "REJECT", signal
        assert rec["incomplete_reason"] == code, (signal, rec)


def test_p_exec_skip_missing_entry_never_carries():
    tdata = make_tdata([(D2, flat_bars, {"skip_mods": (730,)})])
    rec = a24.execute_candidate_signal(exec_signal(D2), tdata, COSTS)
    assert rec["status"] == "SKIP"
    assert rec["incomplete_reason"] == "missing_entry"


def test_p_exec_incomplete_keeps_net_unknown_not_zero():
    tdata = make_tdata([(D2, flat_bars, {"skip_mods": (780,)})])
    rec = a24.execute_candidate_signal(exec_signal(D2), tdata, COSTS)
    assert rec["status"] == "INCOMPLETE"
    assert rec["incomplete_reason"] == "path_gap_after_entry"
    assert rec["exit_ts"] == "" and rec["net"] == "" and rec["exit_base"] == ""
    assert rec["entry_base"] != ""


def test_p_exec_filled_carries_all_fields():
    tdata = make_tdata([(D2, flat_bars, {})])
    rec = a24.execute_candidate_signal(exec_signal(D2), tdata, COSTS)
    assert rec["status"] == "FILLED"
    assert rec["exit_reason"] == "time_exit_18_30"
    assert rec["incomplete_reason"] == "" and rec["detail"] == ""
    assert math.isclose(rec["net"], rec["gross"] - rec["costs_total"],
                        abs_tol=ABS_TOL)


# ---------------------------------------------------------------- P: costs/slippage

def test_p_costs_adverse_direction_long_short():
    costs = a7.CostParams(commission_bps=10.0, slippage_bps=10.0)
    assert costs.fill(100.0, 1) > 100.0
    assert costs.fill(100.0, -1) < 100.0
    assert costs.commission(100.0) == pytest.approx(0.1, abs=ABS_TOL)


def test_p_costs_validation_rejects():
    with pytest.raises(ValueError):
        a7.CostParams(-1.0, 0.0).validate()
    with pytest.raises(ValueError):
        a7.CostParams(0.0, 10000.0).validate()
    assert ZERO_COSTS.gross_only is True
    assert COSTS.gross_only is False


def test_p_costs_nonfinite_result_raises():
    tdata = make_tdata([(D2, flat_bars, {})])
    bad = a7.CostParams(float("inf"), 0.0)
    rec = a24.execute_candidate_signal(exec_signal(D2), tdata, bad)
    assert rec["status"] == "REJECT"
    assert rec["incomplete_reason"] == "invalid_costs"
    live = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                       (D2, drift_bars, {})])
    with pytest.raises(ValueError):
        a7.run_day(live, a7.CONFIGS[0], D2, bad)


# ---------------------------------------------------------------- P: causality

def test_p_future_bars_do_not_change_signal_or_entry():
    base_days = [(D0, drift_bars, {}), (D1, drift_bars, {}),
                 (D2, drift_bars, {})]
    tdata = make_tdata(base_days)
    rec = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    extra = drift_bars(date(2026, 6, 18), start=500.0, slope=1.0)
    bars = []
    for _day, kind, kwargs in base_days:
        bars.extend(kind(_day, **kwargs))
    tdata2 = a7.build_ticker_data(TICKER, bars + extra, D0, [])
    rec2 = a7.run_day(tdata2, a7.CONFIGS[0], D2, COSTS)
    for field in ("side", "decision_bar_ts", "signal_time", "entry_ts",
                  "entry_base", "atr", "sl_level", "status"):
        assert rec[field] == rec2[field]


def test_p_summarize_counts_filled_only():
    filled = {"ticker": TICKER, "config": "S1-A", "side": "long",
              "status": "FILLED", "gross": 3.0, "costs_total": 1.0, "net": 2.0}
    filled2 = dict(filled, gross=-4.0, costs_total=1.0, net=-5.0)
    incomplete = dict(filled, status="INCOMPLETE", gross="", costs_total="",
                      net="")
    rows = a7.summarize([filled, filled2, incomplete])
    assert len(rows) == 1
    row = rows[0]
    assert row["filled"] == 2 and row["incomplete"] == 1
    assert row["gross_sum"] == pytest.approx(-1.0, abs=ABS_TOL)
    assert row["net_sum"] == pytest.approx(-3.0, abs=ABS_TOL)


def test_p_determinism_repeat_calls_identical():
    tdata = make_tdata([(D0, drift_bars, {}), (D1, drift_bars, {}),
                        (D2, drift_bars, {})])
    r1 = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    r2 = a7.run_day(tdata, a7.CONFIGS[0], D2, COSTS)
    assert r1 == r2
    sig = exec_signal(D2)
    e1 = a24.execute_candidate_signal(sig, tdata, COSTS)
    e2 = a24.execute_candidate_signal(sig, tdata, COSTS)
    assert e1 == e2


def test_p_both_shells_same_contract_identical_result():
    """A7 S2-A run_day vs A24 execute on the same fade day: the shared kernel
    must produce byte-identical decision/entry/exit/accounting fields."""
    import day_candidate_execution as a24mod
    d1 = flat_bars(D1, price=100.0)
    d2 = []
    tick = a7.label_ts(D2, "10:00")
    end = a7.label_ts(D2, "18:44")
    while tick <= end:
        d2.append(a7.MinuteBar(tick, 101.0, 101.005, 100.995, 101.0, 10.0))
        tick += timedelta(minutes=1)
    tdata = a7.build_ticker_data(TICKER, drift_bars(D0, slope=0.005) + d1 + d2,
                                 D0, [])
    cfg = a7.CONFIGS[2]  # S2-A: decision 10:20, entry 10:30
    rec_a7 = a7.run_day(tdata, cfg, D2, COSTS)
    assert rec_a7["status"] == "FILLED"
    dec_ts = a7.label_ts(D2, "10:20")
    atr, n_tr = a7.atr_before(tdata, dec_ts)
    assert n_tr >= 14
    ie = a7.label_ts(D2, "10:30").isoformat()
    signal = {
        "type": "signal", "candidate_id": "C2", "ticker": TICKER,
        "day": D2.isoformat(), "spec_hash": a24mod.CANDIDATE_SPEC_HASH,
        "side": rec_a7["side"], "atr": atr, "decision_label": "10:20",
        "decision_close_time": ie, "intended_entry_time": ie,
        "observed_until": ie,
    }
    rec_a24 = a24mod.execute_candidate_signal(signal, tdata, COSTS)
    assert rec_a24["status"] == "FILLED"
    for field in ("side", "entry_ts", "entry_base", "entry_fill", "slip_entry",
                  "commission_entry", "sl_level", "tp_level", "exit_ts",
                  "exit_base", "exit_fill", "slip_exit", "commission_exit",
                  "exit_reason", "gross", "costs_total", "net", "status"):
        assert rec_a7[field] == rec_a24[field], field


# ---------------------------------------------------------------- F: failing-first (step-2 fixes)

def _research():
    try:
        from app.engine import research_execution as rx
    except ImportError as exc:
        pytest.fail(f"research_execution module missing (step-2 fix): {exc}")
    return rx


def test_f_common_core_walk_matches_a7_on_matrix():
    rx = _research()
    tdata = _walk_tdata({780: None})
    entry_ts = a7.label_ts(D2, "12:10")
    base = tdata.minute_by_ts[entry_ts].open
    cases = [
        (1, base - 0.1, None),
        (-1, base + 0.1, None),
        (1, base - 0.1, base + 0.1),
        (1, base - 5.0, None),
    ]
    for side, sl, tp in cases:
        expected = a7._walk(tdata, D2, entry_ts, side, sl, tp)
        got = rx.walk_exit(tdata.minutes_by_day.get(D2, []),
                           tdata.minute_by_ts, day=D2, entry_ts=entry_ts,
                           side=side, sl=sl, tp=tp,
                           full_18_20=a7._full_bar(tdata, D2, "18:20") is not None,
                           has_18_30=tdata.minute_by_ts.get(a7.label_ts(D2, "18:30")) is not None)
        assert got == expected, (side, sl, tp)


def test_f_common_core_none_branch_matches_both_shells():
    rx = _research()
    days = [(D0, drift_bars, {"start": 100.0, "slope": 0.05}),
            (D1, drift_bars, {"start": 100.0, "slope": 0.05})]
    bars = []
    for day, kind, kwargs in days:
        bars.extend(kind(day, **kwargs))
    tick = a7.label_ts(D2, "10:00")  # day cut at 18:25: plan window entered,
    end = a7.label_ts(D2, "18:25")   # 18:20 bar not full, 18:30 absent
    i = 0
    while tick <= end:
        c = 100.0 + 0.05 * i
        bars.append(a7.MinuteBar(tick, c, c + 0.005, c - 0.005, c, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    probe = a7.build_ticker_data(TICKER, bars, D0, [])
    entry_ts = a7.label_ts(D2, "12:10")
    base = probe.minute_by_ts[entry_ts].open
    assert a7._walk(probe, D2, entry_ts, 1, base - 5.0, None) is None
    day_minutes = probe.minutes_by_day.get(D2, [])
    expected = "time_exit_bar_18_20_incomplete"
    assert rx.none_incomplete(
        day_minutes, full_18_20=False,
        has_18_30=False) == expected
    rec = a7.run_day(probe, a7.CONFIGS[0], D2, COSTS)
    assert rec["status"] == "INCOMPLETE"
    assert rec["incomplete_reason"] == expected
    assert not hasattr(a24, "_none_incomplete"), \
        "EXEC duplicate _none_incomplete must be removed (delegate to core)"


def test_f_common_core_settle_matches_shells_exactly():
    rx = _research()
    tdata = make_tdata([(D2, flat_bars, {})])
    rec = a24.execute_candidate_signal(exec_signal(D2), tdata, COSTS)
    out_entry = rx.settle_entry(rec["entry_base"], 1, COSTS)
    for field in ("entry_fill", "slip_entry", "commission_entry"):
        assert out_entry[field] == pytest.approx(rec[field], abs=ABS_TOL), field
    out_exit = rx.settle_exit(rec["entry_base"], rec["exit_base"], 1, COSTS,
                              entry_fill=out_entry["entry_fill"],
                              commission_entry=out_entry["commission_entry"])
    for field in ("exit_fill", "slip_exit", "commission_exit", "gross",
                  "net", "costs_total"):
        assert out_exit[field] == pytest.approx(rec[field], abs=ABS_TOL), field


def test_f_replay_schema_is_single_sourced():
    import day_candidate_replay as replay
    assert replay.TRADE_FIELDS is a7.TRADE_FIELDS
    assert replay.SKIP_FIELDS is a7.SKIP_FIELDS


def test_f_replay_configs_detail_uses_engine_mults():
    import day_candidate_replay as replay
    src = Path(replay.__file__).read_text(encoding="utf-8")
    assert "configs_detail" in src
    assert replay_inspect_mults() == (a7.SL_ATR_MULT, a7.TP_ATR_MULT)


def replay_inspect_mults():
    """No hard-coded SL/TP multiplier literals inside configs_detail dicts."""
    import ast
    import day_candidate_replay as replay
    tree = ast.parse(Path(replay.__file__).read_text(encoding="utf-8"))
    hard = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant)]
            if "sl_atr_mult" in keys or "tp_atr_mult" in keys:
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant)
                            and k.value in ("sl_atr_mult", "tp_atr_mult")
                            and isinstance(v, ast.Constant)):
                        hard.append((k.value, v.value))
    assert not hard, f"hard-coded SL/TP mults in configs_detail: {hard}"
    return (a7.SL_ATR_MULT, a7.TP_ATR_MULT)


def test_f_replay_outside_window_events_are_recorded(tmp_path, monkeypatch):
    import day_candidate_replay as replay
    rows = []
    for day, kind in ((D0, "flat"), (D1, "break")):
        tick = a7.label_ts(day, "10:00")
        end = a7.label_ts(day, "18:44")
        i = 0
        while tick <= end:
            mod = a7.msk_mod(tick)
            if kind == "flat" or mod < 630:
                price = 100.0
            elif mod < 640:
                price = 100.0 + 0.05 * (mod - 629)
            else:
                price = 100.5
            rows.append(minute_row(TICKER, tick, price))
            tick += timedelta(minutes=1)
            i += 1
    src = tmp_path / "m.csv"
    write_csv(src, rows)
    out = tmp_path / "run"
    argv = ["replay", "--input", str(src), "--ticker", TICKER,
            "--candidates", "C1,C2", "--commission-bps", "5",
            "--slippage-bps", "2", "--warmup-start", D0.isoformat(),
            "--eval-start", "2026-06-22", "--eval-end", "2026-06-23",
            "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    assert replay.main() == 0
    with (out / "skipped.csv").open(encoding="utf-8") as fh:
        skipped = list(csv.DictReader(fh))
    reasons = {r["reason"] for r in skipped}
    assert "outside_eval_window" in reasons, reasons
    assert "insufficient_context" in reasons, reasons
