"""B3 тесты summarize_batch15_r2.py (герметично, на temp-артефактах).

Проверяют: восстановление классификации insufficient_context по явному отображению
reason->type, UNKNOWN для неизвестных причин, разделение costs, обе метрики
(equal-ticker / pooled-trade), концентрацию по тикеру, отсутствие скрытия
MISSING/UNKNOWN. Никаких реальных данных/сети.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import summarize_batch15_r2 as sb2  # noqa: E402

TRADE_FIELDS = ("ticker", "candidate_id", "day", "side", "status",
                "entry_base", "net")
SKIP_FIELDS = ("ticker", "candidate_id", "day", "reason", "detail")


def _write_cell(root: Path, group: str, cid: str, ticker: str, cost_id: str,
                *, filled=(), incomplete=(), skips=()) -> None:
    cell = root / group / "20260101T0000Z" / f"{cid}__{ticker}__{cost_id}"
    cell.mkdir(parents=True, exist_ok=True)
    trades = [{"ticker": ticker, "candidate_id": cid, "day": d,
               "side": "long", "status": "FILLED", "entry_base": eb, "net": n}
              for d, eb, n in filled]
    trades += [{"ticker": ticker, "candidate_id": cid, "day": d,
                "side": "long", "status": "INCOMPLETE", "entry_base": "100",
                "net": ""} for d in incomplete]
    with (cell / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        w.writeheader()
        w.writerows(trades)
    with (cell / "skipped.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SKIP_FIELDS)
        w.writeheader()
        w.writerows([{"ticker": ticker, "candidate_id": cid, "day": d,
                      "reason": r, "detail": ""} for d, r in skips])
    (cell / "preflight.json").write_text(
        json.dumps({"spec_hash": "a" * 64}), encoding="utf-8")


def _full(root: Path, cid: str, **kw) -> None:
    group = ("b1" if cid in ("B01", "B02", "B03", "B04", "B05")
             else "b2" if cid in ("B06", "B07", "B08", "B09", "B10")
             else "b3")
    for tk in sb2.TICKERS:
        for cost in sb2.COST_IDS:
            _write_cell(root, group, cid, tk, cost, **kw)


def test_insufficient_context_recovered_not_zero(tmp_path):
    root = tmp_path / "b"
    _full(root, "B11",
          filled=[("2026-06-12", "100.0", "0.5")],
          incomplete=["2026-06-15"],
          skips=[("2026-06-16", "incomplete_window"),
                 ("2026-06-17", "incomplete_window"),
                 ("2026-06-18", "no_signal")])
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 30
    r = next(x for x in rows if x["candidate_id"] == "B11"
             and x["cost_scenario"] == "c5_s2")
    assert int(r["insufficient_context"]) == 8, r
    assert int(r["SKIP"]) == 4, r
    assert int(r["UNKNOWN"]) == 0
    assert r["coverage_status"] == "OK"


def test_unknown_reason_counted_not_invented(tmp_path):
    root = tmp_path / "b"
    _full(root, "B12",
          filled=[("2026-06-12", "100.0", "0.5")],
          skips=[("2026-06-16", "weird_new_reason"),
                 ("2026-06-17", "no_signal")])
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    r = next(x for x in rows if x["candidate_id"] == "B12"
             and x["cost_scenario"] == "c5_s2")
    assert int(r["UNKNOWN"]) == 4, r
    assert "weird_new_reason" in r["unknown_reasons"]
    assert int(r["SKIP"]) == 4
    assert int(r["insufficient_context"]) == 0
    assert "UNKNOWN_REASONS" in r["coverage_status"]


def test_pooled_and_equal_ticker_differ(tmp_path):
    root = tmp_path / "b"
    group = "b1"
    # GAZP: 3 убыточные; LKOH: 1 прибыльная -> pooled отриц., equal-ticker зависит
    for tk, filled in (
        ("GAZP", [("2026-06-12", "100.0", "-0.5"), ("2026-06-13", "100.0", "-0.5"),
                  ("2026-06-15", "100.0", "-0.5")]),
        ("LKOH", [("2026-06-12", "100.0", "1.0")]),
        ("SFIN", []), ("VKCO", []),
    ):
        for cost in sb2.COST_IDS:
            _write_cell(root, group, "B01", tk, cost, filled=filled)
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    r = next(x for x in rows if x["candidate_id"] == "B01"
             and x["cost_scenario"] == "c5_s2")
    pooled = float(r["pooled_net_mean_per_entry"])
    equal = float(r["equal_ticker_net_mean_per_entry"])
    assert pooled < 0, pooled
    assert equal > 0, equal
    assert int(r["FILLED"]) == 4
    assert r["per_ticker_filled"] == json.dumps({"GAZP": 3, "LKOH": 1},
                                                sort_keys=True)


def test_concentration_max_share(tmp_path):
    root = tmp_path / "b"
    for tk, n in (("GAZP", 3), ("LKOH", 1), ("SFIN", 0), ("VKCO", 0)):
        filled = [(f"2026-06-{12 + i:02d}", "100.0", "0.1") for i in range(n)]
        for cost in sb2.COST_IDS:
            _write_cell(root, "b1", "B02", tk, cost, filled=filled)
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    r = next(x for x in rows if x["candidate_id"] == "B02"
             and x["cost_scenario"] == "c5_s2")
    assert r["max_ticker"] == "GAZP"
    assert abs(float(r["max_ticker_share"]) - 0.75) < 1e-9


def test_missing_cell_reported(tmp_path):
    root = tmp_path / "b"
    for cid in sb2.CANDIDATE_IDS:
        group = ("b1" if cid in ("B01", "B02", "B03", "B04", "B05")
                 else "b2" if cid in ("B06", "B07", "B08", "B09", "B10")
                 else "b3")
        for tk in sb2.TICKERS:
            for cost in sb2.COST_IDS:
                _write_cell(root, group, cid, tk, cost,
                            filled=[("2026-06-12", "100.0", "0.1")])
    import shutil
    shutil.rmtree(root / "b3" / "20260101T0000Z" / "B13__SFIN__c5_s2")
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    r = next(x for x in rows if x["candidate_id"] == "B13"
             and x["cost_scenario"] == "c5_s2")
    assert int(r["FILLED"]) == 3
    assert "PARTIAL_COVERAGE" in r["coverage_status"]
    md = (out / "RESULTS_R2.md").read_text(encoding="utf-8")
    assert "B13|SFIN|c5_s2" in md


def test_incomplete_not_summed_as_unique(tmp_path):
    root = tmp_path / "b"
    _full(root, "B14", filled=[("2026-06-12", "100.0", "0.1")],
          incomplete=["2026-06-15", "2026-06-16"])
    out = tmp_path / "out"
    rc = sb2.main(["--root", str(root), "--out", str(out)])
    assert rc == 0
    with (out / "results_r2.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for cost in sb2.COST_IDS:
        r = next(x for x in rows if x["candidate_id"] == "B14"
                 and x["cost_scenario"] == cost)
        assert int(r["INCOMPLETE"]) == 8, r
