"""C3_TREND60_V1 (Builder-2, WORKLINE-2H-20261011): постоянные тесты кандидата.

Герметично: синтетика в tmp_path, только offline (без сети/БД/DeepSeek). Пин
spec_hash, rising/falling/flat, неполный/дублированный час -> insufficient_context
(никогда не no_signal), ATR warmup/invalid, причинность при изменении будущих
баров, точная минута входа 11:00, полная цепочка signal->A24->ledger, регистрация
spec в A24 и неизменность C1/C2, повторяемость ledger, CLI single+aggregate.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_candidate_execution as a24
import day_strategies_core as a7
import intraday_trend_candidate as c3
import run_intraday_trend as runner

TICKER = "TST"
W0 = date(2026, 6, 15)  # Mon
W1 = date(2026, 6, 16)
W2 = date(2026, 6, 17)
D = date(2026, 6, 18)   # Thu decision day
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)
ABS_TOL = 1e-9

PINNED_SPEC_HASH = c3.TREND_SPEC_HASH


def _bars_day(day, price_fn, volume=10.0, skip_mods=()):
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod not in skip_mods:
            p = price_fn(mod, i)
            bars.append(a7.MinuteBar(tick, p, p + 0.005, p - 0.005, p, volume))
        tick += timedelta(minutes=1)
        i += 1
    return bars


def _drift_price(base=100.0, slope=0.02):
    return lambda mod, i: base + slope * i


def _hour_price(hour_closes, base=100.0, drift=0.0005):
    def pf(mod, i):
        if 600 <= mod < 660:
            return hour_closes[(mod - 600) // 10]
        return base + drift * i
    return pf


def _warmup():
    return (_bars_day(W0, _drift_price()) + _bars_day(W1, _drift_price()) +
            _bars_day(W2, _drift_price()))


def _tdata(extra_bars=(), warmup_start=W0, decision_bars=None):
    bars = list(_warmup()) + list(extra_bars)
    if decision_bars is not None:
        bars += decision_bars
    issues: list = []
    return a7.build_ticker_data(TICKER, bars, warmup_start, issues)


def _decision_tdata(hour_closes, after=None, skip_mods=(), mutate_1050=None):
    hp = _hour_price(hour_closes)

    def pf(mod, i):
        if after is not None and mod >= 660:
            return after(mod, i)
        return hp(mod, i)

    bars = _bars_day(D, pf, skip_mods=skip_mods)
    if mutate_1050 is not None:
        o, h, l, c = mutate_1050
        bars = [b if a7.msk_mod(b.ts) != 650 else a7.MinuteBar(b.ts, o, h, l, c, b.volume)
                for b in bars]
    return _tdata(decision_bars=bars)


def _zero_bars_day(day, price_fn):
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    i = 0
    while tick <= end:
        p = price_fn(a7.msk_mod(tick), i)
        bars.append(a7.MinuteBar(tick, p, p, p, p, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    return bars


def _zero_hour_price(hour_closes, flat=100.0):
    def pf(mod, i):
        if 600 <= mod < 660:
            return hour_closes[(mod - 600) // 10]
        return flat
    return pf


RISING = [100.0, 100.1, 100.2, 100.3, 100.4, 100.6]
FALLING = [100.6, 100.5, 100.4, 100.3, 100.2, 100.0]
FLAT = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
ZERO_RISING = [100.0, 100.0, 100.0, 100.0, 100.0, 100.1]


# ------------------------------------------------------------- contract / hash

def test_spec_hash_is_pinned_and_fields_frozen():
    assert PINNED_SPEC_HASH == c3.TREND_SPEC_HASH
    assert c3.SPEC["candidate_id"] == "C3"
    assert c3.SPEC["name"] == "C3_TREND60_V1"
    assert c3.SPEC["decision_msk"] == "11:00"
    assert c3.SPEC["decision_label"] == "10:50"
    assert c3.SPEC["entry_label"] == "11:00"
    assert list(c3.SPEC["hour_labels_msk"]) == ["10:00", "10:10", "10:20",
                                                "10:30", "10:40", "10:50"]
    assert c3.SPEC["atr_excludes_deciding_bar"] is True
    assert c3.SPEC["sl_atr_mult"] == 2.0 and c3.SPEC["tp_atr_mult"] == 3.0
    assert c3.spec_hash() == PINNED_SPEC_HASH


# ------------------------------------------------------------- direction rules

def test_rising_hour_is_long_at_exact_11_00():
    tdata = _decision_tdata(RISING)
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "signal" and ev["side"] == "long"
    assert ev["decision_label"] == "10:50"
    ie = a7.label_ts(D, "11:00").isoformat()
    assert ev["intended_entry_time"] == ie
    assert ev["decision_close_time"] == ie
    assert ev["atr"] > 0
    assert ev["gates"]["decision_close"] == pytest.approx(100.6, abs=ABS_TOL)
    assert ev["gates"]["hour_mean"] == pytest.approx(sum(RISING) / 6.0, abs=ABS_TOL)


def test_falling_hour_is_short():
    tdata = _decision_tdata(FALLING)
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "signal" and ev["side"] == "short"


def test_flat_hour_is_no_signal_skip():
    tdata = _decision_tdata(FLAT)
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


def test_equality_within_float_eps_is_no_signal():
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0 + 1e-13]
    ev = c3.evaluate_trend_day(_decision_tdata(closes), D)
    assert ev["type"] == "skip" and ev["reason"] == "no_signal"


# ------------------------------------------------------------- unknown != no_signal

def test_missing_hour_minute_is_insufficient_context():
    tdata = _decision_tdata(RISING, skip_mods=(625,))  # 10:25 dropped -> 10:20 bar short
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "insufficient_context"
    assert ev["reason"] == "incomplete_hour"
    assert ev["type"] != "skip"


def test_conflicting_duplicate_minute_drops_hour_to_insufficient(tmp_path):
    rows = []
    for b in _bars_day(D, _hour_price(RISING)):
        rows.append(_row(b))
    dup = [r for r in rows if r["ts"].startswith(
        a7.label_ts(D, "10:25").strftime("%Y-%m-%dT%H:%M"))][0]
    bad = dict(dup)
    bad["close"] = "999.0"
    bad["high"] = "999.0"
    rows.append(bad)
    path = tmp_path / "m.csv"
    _write_rows(path, rows)
    by_ticker, issues, _sha = a7.load_minute_csv(path)
    assert any(i.code == "duplicate_conflicting" for i in issues)
    tdata = a7.build_ticker_data(TICKER, by_ticker[TICKER], W0, [])
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "insufficient_context" and ev["reason"] == "incomplete_hour"


# ------------------------------------------------------------- ATR gates

def test_atr_warmup_insufficient_without_history():
    tdata = _tdata(decision_bars=_bars_day(D, _hour_price(RISING)), warmup_start=D)
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "insufficient_context"
    assert ev["reason"] == "atr_warmup_insufficient"


def test_atr_invalid_on_zero_range_warmup():
    warm = (_zero_bars_day(W0, lambda m, i: 100.0) +
            _zero_bars_day(W1, lambda m, i: 100.0) +
            _zero_bars_day(W2, lambda m, i: 100.0))
    bars = warm + _zero_bars_day(D, _zero_hour_price(ZERO_RISING))
    tdata = a7.build_ticker_data(TICKER, bars, W0, [])
    ev = c3.evaluate_trend_day(tdata, D)
    assert ev["type"] == "insufficient_context" and ev["reason"] == "atr_invalid"


def test_atr_excludes_deciding_bar():
    base = c3.evaluate_trend_day(_decision_tdata(RISING), D)
    mutated = _decision_tdata(RISING, mutate_1050=(100.6, 999.0, 0.5, 100.6))
    other = c3.evaluate_trend_day(mutated, D)
    assert other["type"] == "signal"
    assert other["atr"] == base["atr"]


# ------------------------------------------------------------- causality

def test_future_bars_do_not_change_signal():
    ev1 = c3.evaluate_trend_day(_decision_tdata(RISING), D)
    after = lambda mod, i: 500.0 + 5.0 * i  # wildly different path after 10:59
    ev2 = c3.evaluate_trend_day(_decision_tdata(RISING, after=after), D)
    assert ev1 == ev2


# ------------------------------------------------------------- chain to ledger

def test_signal_executes_via_a24_to_filled_ledger():
    tdata = _decision_tdata(RISING)
    ev = c3.evaluate_trend_day(tdata, D)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS, expected_spec_hash=PINNED_SPEC_HASH)
    assert rec["status"] == "FILLED"
    entry_ts = a7.label_ts(D, "11:00")
    assert rec["entry_ts"] == entry_ts.isoformat()
    assert rec["entry_base"] == pytest.approx(tdata.minute_by_ts[entry_ts].open, abs=ABS_TOL)
    assert math.isclose(rec["net"], rec["gross"] - rec["costs_total"], abs_tol=ABS_TOL)
    assert rec["sl_level"] == pytest.approx(rec["entry_base"] - 2.0 * rec["atr"], abs=ABS_TOL)
    assert rec["tp_level"] == pytest.approx(rec["entry_base"] + 3.0 * rec["atr"], abs=ABS_TOL)


def test_ledger_repeatable_same_interpreter():
    tdata = _decision_tdata(RISING)
    ev = c3.evaluate_trend_day(tdata, D)
    r1 = a24.execute_candidate_signal(ev, tdata, COSTS, expected_spec_hash=PINNED_SPEC_HASH)
    r2 = a24.execute_candidate_signal(ev, tdata, COSTS, expected_spec_hash=PINNED_SPEC_HASH)
    assert r1 == r2


# ------------------------------------------------------------- A24 registry

def test_old_c1c2_spec_hash_and_registration_unchanged():
    assert a24.CANDIDATE_SPEC_HASH == (
        "2a1ecab0ce437f643bae2a541a7abc306728c2aa1347f78e6a7ed27ae876f9e7")
    assert a24.CANDIDATE_SPEC_HASHES["C1"] == a24.CANDIDATE_SPEC_HASH
    assert a24.CANDIDATE_SPEC_HASHES["C2"] == a24.CANDIDATE_SPEC_HASH
    assert a24.CANDIDATE_SPEC_HASHES["C3"] == PINNED_SPEC_HASH
    assert "C3" in a24.CANDIDATES


def test_unknown_candidate_still_rejected():
    tdata = _decision_tdata(RISING)
    ev = c3.evaluate_trend_day(tdata, D)
    ev = {**ev, "candidate_id": "C9"}
    rec = a24.execute_candidate_signal(ev, tdata, COSTS)
    assert rec["status"] == "REJECT" and rec["incomplete_reason"] == "unknown_candidate"


def test_c3_spec_hash_mismatch_and_wrong_pin_rejected():
    tdata = _decision_tdata(RISING)
    ev = c3.evaluate_trend_day(tdata, D)
    tampered = {**ev, "spec_hash": a24.CANDIDATE_SPEC_HASH}
    rec = a24.execute_candidate_signal(tampered, tdata, COSTS,
                                       expected_spec_hash=PINNED_SPEC_HASH)
    assert rec["status"] == "REJECT" and rec["incomplete_reason"] == "spec_hash_mismatch"
    bad_pin = a24.execute_candidate_signal(ev, tdata, COSTS,
                                           expected_spec_hash=a24.CANDIDATE_SPEC_HASH)
    assert bad_pin["status"] == "REJECT"
    assert bad_pin["incomplete_reason"] == "spec_hash_mismatch"


def test_c1_signal_still_executes_on_default_path():
    tdata = _tdata(decision_bars=_bars_day(D, _drift_price(base=100.0, slope=0.0)))
    ie = a7.label_ts(D, "12:10").isoformat()
    signal = {
        "type": "signal", "candidate_id": "C1", "ticker": TICKER,
        "day": D.isoformat(), "spec_hash": a24.CANDIDATE_SPEC_HASH,
        "side": "long", "atr": 0.5, "decision_label": "12:00",
        "decision_close_time": ie, "intended_entry_time": ie, "observed_until": ie,
    }
    rec = a24.execute_candidate_signal(signal, tdata, COSTS)
    assert rec["status"] == "FILLED"


# ------------------------------------------------------------- CLI

def _row(bar):
    return {"ticker": TICKER, "ts": bar.ts.isoformat(), "open": bar.open,
            "high": bar.high, "low": bar.low, "close": bar.close,
            "volume": bar.volume}


def _write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(a7.REQUIRED_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _csv_with_days(path, days):
    rows = []
    for day, price_fn in days:
        rows.extend(_row(b) for b in _bars_day(day, price_fn))
    _write_rows(path, rows)


def test_cli_single_writes_artifacts_and_aggregate(tmp_path):
    src = tmp_path / "TST.csv"
    _csv_with_days(src, [(W0, _drift_price()), (W1, _drift_price()),
                         (W2, _drift_price()), (D, _hour_price(RISING))])
    root = tmp_path / "runs"
    common = ["--input", str(src), "--ticker", TICKER,
              "--warmup-start", W0.isoformat(), "--eval-start", D.isoformat(),
              "--eval-end", D.isoformat()]
    out1 = root / "TST__c5_s2"
    out2 = root / "TST__c10_s5"
    assert runner.main(["single", *common, "--commission-bps", "5",
                        "--slippage-bps", "2", "--out", str(out1),
                        "--spec-hash", PINNED_SPEC_HASH]) == 0
    assert runner.main(["single", *common, "--commission-bps", "10",
                        "--slippage-bps", "5", "--out", str(out2)]) == 0
    for out in (out1, out2):
        for name in ("preflight.json", "manifest.json", "summary.json",
                     "trades.csv", "skipped.csv", "signals.json", "dq.csv"):
            assert (out / name).exists(), name
    pre = json.loads((out1 / "preflight.json").read_text(encoding="utf-8"))
    assert pre["spec_hash"] == PINNED_SPEC_HASH
    summary = json.loads((out1 / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["days_evaluated"] == 1
    assert summary["counts"]["signals"] == 1
    assert summary["counts"]["filled"] == 1
    manifest = json.loads((out1 / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["spec_hash"] == PINNED_SPEC_HASH
    assert manifest["config_hash"]

    agg_dir = tmp_path / "agg"
    assert runner.main(["aggregate", "--runs-root", str(root),
                        "--out-dir", str(agg_dir)]) == 0
    combined = json.loads((agg_dir / "combined_summary.json").read_text(encoding="utf-8"))
    assert combined["spec_hash"] == PINNED_SPEC_HASH
    assert "распределение" in combined["note"].lower()
    with (agg_dir / "summary_by_ticker_month_cost.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["costs"] for r in rows} == {"5.0/2.0bps", "10.0/5.0bps"}
    assert all(r["ticker"] == TICKER for r in rows)


def test_cli_rejects_nonempty_out(tmp_path):
    src = tmp_path / "T.csv"
    _csv_with_days(src, [(W0, _drift_price()), (W1, _drift_price()),
                         (W2, _drift_price()), (D, _hour_price(RISING))])
    out = tmp_path / "run"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        runner.main(["single", "--input", str(src), "--ticker", "T",
                     "--commission-bps", "5", "--slippage-bps", "2",
                     "--warmup-start", W0.isoformat(), "--eval-start", D.isoformat(),
                     "--eval-end", D.isoformat(), "--out", str(out)])
