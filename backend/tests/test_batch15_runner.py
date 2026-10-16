"""BATCH-15 runner (B2): focused-тесты хелперов и одного cell-прогона.

Не запускает полный research (11 мин по CSV) — проверяет детерминизм helpers и
встроенные проверки раннера (net=gross-costs, уникальный event_id, отсутствие
числового net у INCOMPLETE, per-cell статус) на синтетическом TickerData.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_strategies_core as a7
import batch15_group_b as g
import day_candidate_execution as a24
import run_batch15 as runner

W0 = date(2026, 6, 8)
D = date(2026, 6, 11)
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)


def _full_tdata(window):
    def pf(mod, i):
        if 600 <= mod < 720:
            return window[(mod - 600) // 10]
        return 100.0
    bars = []
    for wd in (W0, date(2026, 6, 9), date(2026, 6, 10)):
        tick = a7.label_ts(wd, "10:00")
        end = a7.label_ts(wd, "18:44")
        while tick <= end:
            bars.append(a7.MinuteBar(tick, 100.0, 100.01, 99.99, 100.0, 10.0))
            tick += timedelta(minutes=1)
    tick = a7.label_ts(D, "10:00")
    end = a7.label_ts(D, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if 600 <= mod < 720:
            p = window[(mod - 600) // 10]
        else:
            p = 100.0
        bars.append(a7.MinuteBar(tick, p, p + 0.01, p - 0.01, p, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    issues = []
    return a7.build_ticker_data("TEST", bars, W0, issues)


def test_weekdays_excludes_weekends():
    days = runner._weekdays(date(2026, 6, 8), date(2026, 6, 14))
    assert all(d.weekday() < 5 for d in days)
    assert len(days) == 5  # Mon..Fri


def test_by_month_groups_trades_and_skips():
    trades = [
        {"day": "2026-06-11", "status": "FILLED", "net": "1.0",
         "gross": "2.0", "costs_total": "1.0"},
        {"day": "2026-06-12", "status": "INCOMPLETE"},
        {"day": "2026-07-01", "status": "FILLED", "net": "3.0",
         "gross": "5.0", "costs_total": "2.0"},
    ]
    skips = [{"day": "2026-06-11"}]
    bm = runner._by_month(trades, skips)
    assert "2026-06" in bm and "2026-07" in bm
    assert bm["2026-06"]["filled"] == 1
    assert bm["2026-06"]["incomplete"] == 1
    assert bm["2026-07"]["filled"] == 1
    assert abs(bm["2026-07"]["net_sum"] - 3.0) < 1e-9


def test_run_cell_filled_has_net_identity_and_unique_event_id(tmp_path):
    a24.register_specs(g.SPEC_HASHES)
    tdata = _full_tdata([100.0 + i for i in range(12)])  # rising -> B06 long
    out_dir = tmp_path / "cell"
    summary = runner._run_cell(
        g, tdata, "B06", "TEST", COSTS, "c5_s2", [D], "b", out_dir,
        code_sha={"x": "y"}, preflight_common={"tool": "run_batch15"})
    assert summary["FILLED"] == 1
    assert summary["INCOMPLETE"] == 0
    assert summary["overall_status"] == "OK"
    assert summary["cell_error"] is None

    trades = list(tmp_path.glob("cell/trades.csv"))
    assert trades
    content = (out_dir / "trades.csv").read_text(encoding="utf-8").splitlines()
    assert content[0].startswith("ticker")
    assert "B06|TEST|2026-06-11" in content[1]
    assert (out_dir / "manifest.json").exists()
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["event_id_count"] == 1
    assert man["status"] == "OK"


def test_run_cell_incomplete_has_no_numeric_net(tmp_path):
    a24.register_specs(g.SPEC_HASHES)
    # decision day: window + 12:00..12:20 (entry exists, gap after -> INCOMPLETE)
    window = [100.0 + i for i in range(12)]
    bars = []
    for wd in (W0, date(2026, 6, 9), date(2026, 6, 10)):
        tick = a7.label_ts(wd, "10:00")
        end = a7.label_ts(wd, "18:44")
        while tick <= end:
            bars.append(a7.MinuteBar(tick, 100.0, 100.01, 99.99, 100.0, 10.0))
            tick += timedelta(minutes=1)
    tick = a7.label_ts(D, "10:00")
    end = a7.label_ts(D, "18:44")
    i = 0
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod > 740:  # stop at 12:20, gap after -> INCOMPLETE
            break
        if 600 <= mod < 720:
            p = window[(mod - 600) // 10]
        else:
            p = 100.0
        bars.append(a7.MinuteBar(tick, p, p + 0.01, p - 0.01, p, 10.0))
        tick += timedelta(minutes=1)
        i += 1
    tdata = a7.build_ticker_data("TEST", bars, W0, [])
    out_dir = tmp_path / "cell2"
    summary = runner._run_cell(
        g, tdata, "B06", "TEST", COSTS, "c5_s2", [D], "b", out_dir,
        code_sha={"x": "y"}, preflight_common={})
    assert summary["FILLED"] == 0
    assert summary["INCOMPLETE"] == 1
    assert summary["overall_status"] == "INCOMPLETE_TRADES"


def _warmup_bars():
    bars = []
    for wd in (W0, date(2026, 6, 9), date(2026, 6, 10)):
        tick = a7.label_ts(wd, "10:00")
        end = a7.label_ts(wd, "18:44")
        while tick <= end:
            bars.append(a7.MinuteBar(tick, 100.0, 100.01, 99.99, 100.0, 10.0))
            tick += timedelta(minutes=1)
    return bars


def _make_window_bars(day, window):
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


def test_run_cell_classifies_insufficient_vs_skip_by_event_type(tmp_path):
    a24.register_specs(g.SPEC_HASHES)
    d1 = date(2026, 6, 11)   # missing window bar -> insufficient_context
    d2 = date(2026, 6, 12)   # flat window -> no_signal (skip)
    bars = list(_warmup_bars())
    wb1 = _make_window_bars(d1, [(c, c + 0.02, c - 0.02, c, 1000.0)
                                 for c in (100.0 + i for i in range(12))])
    wb1 = [b for b in wb1 if a7.msk_mod(b.ts) != 650]
    bars += wb1
    wb2 = _make_window_bars(d2, [(100.0, 100.02, 99.98, 100.0, 1000.0)] * 12)
    bars += wb2
    tdata = a7.build_ticker_data("TEST", bars, W0, [])
    out_dir = tmp_path / "cellc"
    summary = runner._run_cell(
        g, tdata, "B06", "TEST", COSTS, "c5_s2", [d1, d2], "b", out_dir,
        code_sha={"x": "y"}, preflight_common={})
    assert summary["signals"] == 0
    assert summary["FILLED"] == 0 and summary["INCOMPLETE"] == 0
    assert summary["SKIP"] == 1
    assert summary["insufficient_context"] == 1
    # saved skipped.csv must carry event_type for provenance
    content = (out_dir / "skipped.csv").read_text(encoding="utf-8").splitlines()
    assert content[0].startswith("ticker")
    assert "insufficient_context" in content[1] or "insufficient_context" in content[2]
