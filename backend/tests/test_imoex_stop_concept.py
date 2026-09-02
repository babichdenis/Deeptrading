"""B2b IMOEX-INFORMED STOP тесты (SHADOW counterfactual, read-only)."""
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "imoex_stop_concept.json")) as f:
        return json.load(f)


def test_no_lookahead_note(report: dict) -> None:
    assert "no look-ahead" in report["note"] or "IMOEX <= baseline exit" in report["note"]
    assert "SHADOW" in report["note"]


def test_reconciliation_n_trades(report: dict) -> None:
    """n строк = сделки с IMOEX-path; все три threshold имеют одно n == rows."""
    rows = report["results"]["threshold_10"]["n"]
    assert rows == report["results"]["threshold_20"]["n"] == report["results"]["threshold_30"]["n"]
    assert rows > 0
    # совпадает с числом сделок с path из скрипта
    assert rows == report.get("results", {}).get("baseline", {}).get("count", 0)


def test_thresholds_monotonic(report: dict) -> None:
    """Больший threshold → меньше early exits."""
    e10 = report["results"]["threshold_10"]["early_exits"]
    e20 = report["results"]["threshold_20"]["early_exits"]
    e30 = report["results"]["threshold_30"]["early_exits"]
    assert e10 > e20 > e30


def test_engine_not_changed() -> None:
    import os as _os
    hits = []
    for root in (os.path.join(BACKEND, "app", "engine"),
                 os.path.join(BACKEND, "app", "services")):
        for dp, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(_os.path.join(dp, fn)) as f:
                    if "imoex_stop_concept" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
