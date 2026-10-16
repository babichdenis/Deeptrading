from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
MODULE_PATH = BACKEND / "scripts" / "audit_signal_lab_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("audit_signal_lab_evidence", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _row(**kw):
    base = {
        "event_id": 1, "start_ts": "2026-08-03T09:00:00+00:00",
        "entry_ts": "2026-08-03T09:00:00+00:00", "exit_ts": "2026-08-03T09:05:00+00:00",
        "entry_px": 100.0, "exit_px": 101.0, "net_return": 0.0095, "r_multiple": 1.0,
        "atr_entry": 0.5, "cfg_hash": "d194492c7d93b4cb",
        "regime_label": "NEUTRAL", "regime_session": "day",
        "regime_version": "v2.0", "regime_params_hash": "3f680b2b160d",
        "regime_created_at": "2026-10-04T21:00:00+00:00",
        "side": "BUY", "exit_reason": "tp",
    }
    base.update(kw)
    return base


def test_report_rounding_rules_match_published_columns():
    stats = {
        "n": 108, "wins": 54, "losses": 54, "nulls": 0,
        "sum_win_norm": 0.1179, "sum_loss_norm": -0.14827,
        "pf_norm": 1.161574, "pf_rub": 1.117035,
        "mean_net_pct": 0.042251, "sum_win_rub": 18.42734, "sum_loss_rub": -23.14769,
        "avg_win_rub": 0.83397, "avg_loss_rub": -0.74621, "avg_r": 0.112,
        "sum_net_norm": -0.03037,
    }
    pub = mod.published_columns(stats)
    assert pub["winrate_pct"] == 50.0
    assert pub["pf"] == 1.16
    assert pub["avg_net_pct"] == 0.0423
    assert pub["avg_win_rub"] == 0.83
    assert pub["avg_r"] == 0.112
    deltas = mod.compare_to_published(pub, pub)
    assert all(d["match"] for d in deltas.values())


def test_compare_to_published_detects_mismatch():
    recomputed = {"n": 108, "wins": 54, "pf": 1.16}
    published = {"n": "108", "wins": "54", "pf": "1.15"}
    deltas = mod.compare_to_published(published, recomputed)
    assert deltas["n"]["match"] and deltas["wins"]["match"]
    assert not deltas["pf"]["match"]


def test_pf_norm_vs_pf_rub_diverge_on_price_distortion():
    rows = [
        _row(entry_px=1.0, exit_px=280.21, net_return=279.0094, r_multiple=500.0),
        _row(entry_px=280.12, exit_px=1.0, net_return=-0.99693, r_multiple=-0.01),
        _row(entry_px=280.04, exit_px=279.5, net_return=-0.002, r_multiple=-0.05),
    ]
    stats = mod.cell_stats(rows)
    assert stats["pf_norm"] > 100
    assert stats["pf_rub"] < 1


def test_overlap_and_greedy_filter():
    rows = [
        _row(entry_ts="2026-08-03T09:00:00+00:00", exit_ts="2026-08-03T10:00:00+00:00",
             net_return=0.01),
        _row(entry_ts="2026-08-03T09:30:00+00:00", exit_ts="2026-08-03T09:40:00+00:00",
             net_return=0.02),
        _row(entry_ts="2026-08-03T10:00:00+00:00", exit_ts="2026-08-03T11:00:00+00:00",
             net_return=-0.03),
    ]
    ov = mod.overlap_stats(rows)
    assert ov["events"] == 3
    assert ov["overlapping_entries"] == 1
    assert ov["greedy_count"] == 2
    assert ov["method"] == "approximate_interval_diagnostic"
    assert ov["utc_normalized"] is True
    assert ov["greedy_stats"]["n"] == 2
    assert ov["greedy_stats"]["sum_net_norm"] == 0.01 - 0.03


def test_cell_stats_null_outcomes_counted():
    rows = [_row(net_return=0.01), _row(net_return=None), _row(net_return=-0.02)]
    stats = mod.cell_stats(rows)
    assert stats["n"] == 3
    assert stats["nulls"] == 1
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert abs(stats["sum_net_norm"] - (0.01 - 0.02)) < 1e-12


def test_normalisation_recomputes_net_from_formula():
    rows = [
        _row(entry_px=100.0, exit_px=101.0, net_return=mod.recalc_net(
            100.0, 101.0, "BUY", 0.0004, 2.0)),
        _row(entry_px=200.0, exit_px=199.0, net_return=mod.recalc_net(
            200.0, 199.0, "BUY", 0.0004, 2.0)),
    ]
    check = mod.normalisation_check(rows, "BUY", {"commission_rate": 0.0004,
                                                   "slippage_bps": 2.0})
    assert check["ok"] and check["max_abs_delta"] == 0.0


def test_bad_bar_detection_flags_price_floor_spike():
    good = [{"open": 280.0}, {"open": 281.0}]
    bad = [{"open": 280.0}, {"open": 1.0}, {"open": 279.5}]
    assert mod.scan_bad_bars(good, 280.0) == []
    flagged = mod.scan_bad_bars(bad, 280.0)
    assert len(flagged) == 1 and flagged[0]["open"] == 1.0


def test_period_half_boundary_from_confirmed_period():
    assert mod.period_half_boundary("2026-08-01T00:00:00+00:00",
                                    "2026-08-31T00:00:00+00:00") == "2026-08-16"
    assert mod.period_half_boundary("2026-09-01T00:00:00+00:00",
                                    "2026-09-30T00:00:00+00:00") == "2026-09-15"
    assert mod.period_half_boundary("", "") is None
    assert mod.period_half_boundary("not-a-date", "x") is None


def test_period_stats_split_uses_run_boundary():
    rows = [_row(start_ts="2026-09-05T10:00:00+00:00", net_return=0.01),
            _row(start_ts="2026-09-06T10:00:00+00:00", net_return=-0.02),
            _row(start_ts="2026-09-20T10:00:00+00:00", net_return=0.03)]
    per = mod.period_stats(rows, "2026-09-15")
    assert per["h1_boundary"] == "2026-09-15"
    assert per["halves"] == {"H1": 2, "H2": 1}
    assert per["H1_stats"]["n"] == 2 and per["H2_stats"]["n"] == 1


def test_cell_filters_by_regime_and_session():
    rows = [
        _row(regime_label="NEUTRAL", regime_session="day"),
        _row(regime_label="TREND", regime_session="day"),
        _row(regime_label="NEUTRAL", regime_session="morning"),
    ]
    cell = {"regime": "NEUTRAL", "session": "day"}
    got = mod.cell_filters(cell, rows)
    assert len(got) == 1
    assert got[0]["regime_label"] == "NEUTRAL"


def test_unique_out_dir_never_overwrites(tmp_path):
    first = mod.unique_out_dir(tmp_path)
    first.mkdir(parents=True)
    second = mod.unique_out_dir(tmp_path)
    assert first != second
    assert not second.exists()


def test_file_audit_is_deterministic(tmp_path):
    run_a, run_b = "runaaa-0000-0000-0000-000000000001", "runbbb-0000-0000-0000-000000000002"
    fields = ["engine", "tf", "side", "ticker", "regime", "session", "n", "wins",
              "losses", "winrate_pct", "pf", "avg_net_pct", "avg_win_rub",
              "avg_loss_rub", "avg_r"]
    rows = [
        ["momentum_1bar", "5min", "SELL", "VKCO", "RANGE", "morning",
         "108", "54", "54", "50.0", "1.16", "0.0423", "0.83", "-0.75", "0.112"],
    ]
    files = []
    for run in (run_a, run_b):
        csv_p = tmp_path / f"{run}_cells.csv"
        with open(csv_p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(fields)
            w.writerows(rows)
        md_p = tmp_path / f"{run}_summary.md"
        md_p.write_text(
            "# Отчёт · профиль sl1_tp2\n\n"
            "Ячеек (без weekend/night): 1; исключено: 0.\n", encoding="utf-8")
        files += [csv_p, md_p]
    cell = {"run_id": run_a, "engine": "momentum_1bar", "tf": "5min", "side": "SELL",
            "ticker": "VKCO", "regime": "RANGE", "session": "morning"}
    res1 = mod.file_audit([cell], files)
    res2 = mod.file_audit([cell], files)
    assert res1["cells"][0]["published_row"] is not None
    assert res1["cells"][0]["published_row"]["pf"] == "1.16"
    assert res1 == res2
    assert res1["checks"][run_a]["summary_matches_csv"] is True


def test_manifest_lists_sha256_of_sources(tmp_path):
    src = tmp_path / "source.md"
    src.write_text("evidence", encoding="utf-8")
    manifest = mod.build_manifest([src], {}, {"mode": "file"})
    entry = manifest["files"][0]
    assert entry["sha256"] == mod.sha256_file(src)
    assert entry["size"] == 8
    json.dumps(manifest)


def test_config_hash_reproducible():
    payload = {"engines": ["a", "b"], "code": "x"}
    assert mod.config_hash_from_payload(payload) == mod.config_hash_from_payload(
        dict(reversed(list(payload.items()))))


def test_write_artifacts_creates_expected_files(tmp_path):
    out_dir = tmp_path / "run1"
    results = {
        "mode": "file", "checks": {},
        "cells": [{
            "cell": {"run_id": "r", "engine": "e", "tf": "5min", "side": "BUY",
                     "ticker": "SBER", "regime": "NEUTRAL", "session": "day"},
            "published_row": None, "recomputed": "NOT_VERIFIED",
        }],
    }
    written = mod.write_artifacts(out_dir, {"generated_at": "2026-10-07"}, results,
                                  "audsha" * 8)
    assert "manifest.json" in written and "checks.json" in written
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "cells_recompute.csv").exists()
    with open(out_dir / "cells_recompute.csv", newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert got[0]["ticker"] == "SBER"
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["auditor"]["sha256"] == "audsha" * 8
    try:
        mod.write_artifacts(out_dir, {}, results, "audsha" * 8)
        raise AssertionError("повторная запись в существующий каталог должна падать")
    except FileExistsError:
        pass


def test_ts_key_utc_normalization():
    offset = mod.ts_key("2026-08-03T09:00:00+03:00")
    plain = mod.ts_key("2026-08-03T06:00:00")
    assert offset == plain == "2026-08-03T06:00:00+00:00"
    assert mod.ts_key("2026-08-03 09:05:00") == mod.ts_key("2026-08-03T09:05:00+00:00")
    assert mod.ts_key(None) is None


def test_overlap_equal_boundaries_not_overlap():
    rows = [
        _row(event_id=1, entry_ts="2026-08-03T09:00:00+00:00",
             exit_ts="2026-08-03T09:05:00+00:00"),
        _row(event_id=2, entry_ts="2026-08-03T09:05:00+00:00",
             exit_ts="2026-08-03T09:05:00+00:00"),
    ]
    ov = mod.overlap_stats(rows)
    assert ov["overlapping_entries"] == 0
    assert ov["greedy_count"] == 2


def test_sensitivity_drop_max_win_vs_drop_affected():
    rows = [
        _row(event_id=1, entry_px=1.0, exit_px=280.21, net_return=279.0094),
        _row(event_id=2, entry_px=280.12, exit_px=1.0, net_return=-0.99693),
        _row(event_id=3, entry_px=100.0, exit_px=101.0, net_return=0.001),
    ]
    s = mod.sensitivity(rows, floor=5.0)
    assert s["dropped_affected"]["n"] == 2
    assert s["dropped_affected"]["stats"]["n"] == 1
    assert s["dropped_max_win"]["n"] == 1
    assert s["dropped_max_win"]["event_id"] == 1
    assert s["dropped_max_win"]["stats"]["n"] == 2


def test_regime_availability_note_not_verified():
    note = mod.regime_availability_note(["2026-10-04T21:00:00+00:00"])
    assert note.startswith("NOT_VERIFIED")
    assert "persisted_at" in note


def test_analyze_cell_regime_and_finite_flags():
    rows = [
        _row(net_return=0.01),
        _row(net_return=float("nan"), entry_px=float("nan")),
    ]
    cell = {"run_id": "r", "engine": "e", "tf": "5min", "side": "BUY",
            "ticker": "T", "regime": "NEUTRAL", "session": "day"}
    a = mod.analyze_cell(rows, cell, {"commission_rate": 0.0005, "slippage_bps": 2.0},
                         "2026-08-16", None)
    assert a["finite_ok"] is False
    stats = a["recomputed"]
    assert stats["non_finite_net"] == 1 and stats["non_finite_prices"] == 1
    assert "NOT_VERIFIED" in a["versions"]["regime_causal_availability"]
    assert a["versions"]["regime_persisted_at"] == ["2026-10-04T21:00:00+00:00"]


def test_snapshot_roundtrip_recompute_consistent(tmp_path):
    cell = {"run_id": mod.A_RUN, "engine": "momentum_1bar", "tf": "5min",
            "side": "BUY", "ticker": "SFIN", "regime": "NEUTRAL", "session": "day"}
    rows = [_row(event_id=1, net_return=0.01), _row(event_id=2, net_return=-0.005)]
    costs = {"commission_rate": 0.0005, "slippage_bps": 2.0}
    h1 = "2026-08-16"
    result = mod.analyze_cell(rows, cell, costs, h1, None)
    results = {
        "mode": "db",
        "cells": [result],
        "checks": {"db_readonly": {"run_ids": [mod.A_RUN]}},
        "_snapshot": {
            "cells": [{"cell": cell, "profile": "sl1_tp2", "rows": rows,
                       "costs": costs, "h1_boundary": h1}],
            "matched_candles": [],
        },
    }
    out_dir = tmp_path / "snap1"
    mod.write_artifacts(out_dir, {"generated_at": "2026-10-07T00:00:00+00:00"},
                        results, "audsha" * 8)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_cells_sha256"]
    assert (out_dir / "snapshot_cells.json").exists()
    code, detail = mod.recompute_from_snapshot(out_dir, "audsha" * 8)
    assert code == 0
    assert detail[0]["consistent"] is True
    check = json.loads((out_dir / "snapshot_recompute_check.json")
                       .read_text(encoding="utf-8"))
    assert check["consistent"] is True
    assert check["auditor_sha256"] == "audsha" * 8


def test_config_checks_provenance_not_verified():
    meta = {mod.A_RUN: {"code_version": "", "config_hash": "does-not-match",
                        "engines": ["momentum_1bar"]}}
    out = mod.config_hash_checks(meta, mod.BACKEND)
    entry = out[mod.A_RUN]
    assert entry["config_payload_matches_stored"] is False
    assert entry["file_hash"]
    assert "NOT_VERIFIED" in entry["executed_code_provenance"]
    assert "NOT_VERIFIED" in entry["data_snapshot_provenance"]
    assert entry["commit_exists"] is None
