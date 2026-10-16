"""B3 тесты summarize_batch15.py (герметично, на temp-артефактах).

Проверяют: сборку 15×4×2 ячеек из group-каталогов, корректный вердикт
(positive/negative/insufficient/ERROR), отсутствие скрытия MISSING/ERROR,
формат results.csv и RESULTS.md. Никаких реальных данных/сети.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import summarize_batch15 as sb  # noqa: E402


def _write_cell(root: Path, group: str, cid: str, ticker: str, cost_id: str,
                summary: dict | None = None, error: str | None = None) -> None:
    run_dir = root / group / "20260101T0000Z"
    cell = run_dir / f"{cid}__{ticker}__{cost_id}"
    cell.mkdir(parents=True, exist_ok=True)
    if error is not None:
        (cell / "ERROR.json").write_text(
            json.dumps({"error": error}), encoding="utf-8")
    elif summary is not None:
        (cell / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8")


def _summary(cid: str, ticker: str, cost_id: str, **over) -> dict:
    base = {
        "candidate_id": cid, "ticker": ticker, "cost_scenario": cost_id,
        "days_evaluated": 84, "signals": 80, "FILLED": 78, "INCOMPLETE": 2,
        "SKIP": 4, "insufficient_context": 2, "REJECT": 0,
        "overall_status": "INCOMPLETE_TRADES", "cell_error": None,
        "spec_hash": "a" * 64,
        "filled_stats": {"wins": 40, "losses": 38, "flat": 0,
                         "gross_sum": 1.0, "costs_sum": 0.5, "net_sum": 0.5,
                         "net_mean_per_entry": 0.006, "net_median_per_entry": 0.005},
    }
    base.update(over)
    return base


def test_all_15_candidates_and_120_rows(tmp_path):
    root = tmp_path / "batch15"
    for cid in sb.CANDIDATE_IDS:
        group = "b1" if cid in ("B01", "B02", "B03", "B04", "B05") else (
            "b2" if cid in ("B06", "B07", "B08", "B09", "B10") else "b3")
        for ticker in sb.TICKERS:
            for cost_id in sb.COST_IDS:
                _write_cell(root, group, cid, ticker, cost_id,
                            _summary(cid, ticker, cost_id))
    out = tmp_path / "out"
    rc = sb.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 120, f"ожидалось 120 строк, получено {len(rows)}"
    assert {r["candidate_id"] for r in rows} == set(sb.CANDIDATE_IDS)
    md = (out / "RESULTS.md").read_text(encoding="utf-8")
    for cid in sb.CANDIDATE_IDS:
        assert cid in md


def test_missing_cell_reported_not_hidden(tmp_path):
    root = tmp_path / "batch15"
    for cid in sb.CANDIDATE_IDS:
        group = "b1" if cid in ("B01", "B02", "B03", "B04", "B05") else (
            "b2" if cid in ("B06", "B07", "B08", "B09", "B10") else "b3")
        for ticker in sb.TICKERS:
            for cost_id in sb.COST_IDS:
                if cid == "B01" and ticker == "GAZP" and cost_id == "c5_s2":
                    continue
                _write_cell(root, group, cid, ticker, cost_id,
                            _summary(cid, ticker, cost_id))
    out = tmp_path / "out"
    rc = sb.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 120
    miss = [r for r in rows if r["overall_status"] == "MISSING"]
    assert len(miss) == 1
    assert miss[0]["candidate_id"] == "B01" and miss[0]["ticker"] == "GAZP"
    md = (out / "RESULTS.md").read_text(encoding="utf-8")
    assert "B01|GAZP|c5_s2" in md


def test_error_cell_reported(tmp_path):
    root = tmp_path / "batch15"
    for cid in sb.CANDIDATE_IDS:
        group = "b1" if cid in ("B01", "B02", "B03", "B04", "B05") else (
            "b2" if cid in ("B06", "B07", "B08", "B09", "B10") else "b3")
        for ticker in sb.TICKERS:
            for cost_id in sb.COST_IDS:
                if cid == "B14" and ticker == "SFIN":
                    _write_cell(root, group, cid, ticker, cost_id,
                                error="boom exception")
                else:
                    _write_cell(root, group, cid, ticker, cost_id,
                                _summary(cid, ticker, cost_id))
    out = tmp_path / "out"
    rc = sb.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    err = [r for r in rows
           if r["candidate_id"] == "B14" and r["ticker"] == "SFIN"]
    assert len(err) == 2
    for r in err:
        assert r["overall_status"] == "ERROR"
        assert "boom exception" in r["cell_error"]


def test_verdict_positive_negative_insufficient():
    pos = _summary("B01", "GAZP", "c5_s2",
                   filled_stats={"wins": 50, "losses": 20, "flat": 0,
                                 "gross_sum": 5.0, "costs_sum": 1.0,
                                 "net_sum": 4.0, "net_mean_per_entry": 0.02,
                                 "net_median_per_entry": 0.015})
    neg = _summary("B02", "GAZP", "c5_s2",
                   filled_stats={"wins": 20, "losses": 50, "flat": 0,
                                 "gross_sum": -5.0, "costs_sum": 1.0,
                                 "net_sum": -6.0, "net_mean_per_entry": -0.02,
                                 "net_median_per_entry": -0.015})
    assert sb._verdict([{**pos, "group": "a"}]) == "positive_on_known"
    assert sb._verdict([{**neg, "group": "a"}]) == "negative_on_known"
    assert sb._verdict([{"overall_status": "ERROR"}]) == "ERROR"
    assert sb._verdict([{"overall_status": "MISSING"}]) == "MISSING"
    assert sb._verdict([]) == "insufficient_data"


def test_net_of_different_tickers_not_summed(tmp_path):
    root = tmp_path / "batch15"
    for cid in ("B11",):
        for ticker in sb.TICKERS:
            for cost_id in sb.COST_IDS:
                s = _summary(cid, ticker, cost_id)
                s["filled_stats"]["net_sum"] = 10.0
                _write_cell(root, "b3", cid, ticker, cost_id, s)
    for cid in sb.CANDIDATE_IDS:
        if cid == "B11":
            continue
        for ticker in sb.TICKERS:
            for cost_id in sb.COST_IDS:
                _write_cell(root, "b2", cid, ticker, cost_id,
                            _summary(cid, ticker, cost_id))
    out = tmp_path / "out"
    rc = sb.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    md = (out / "RESULTS.md").read_text(encoding="utf-8")
    assert "НЕ доходность портфеля" in md
    assert "ПАРНЫЕ" in md
    assert "holdout" in md
