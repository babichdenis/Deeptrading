"""B3 ENTRY CONFLUENCE тесты (SHADOW, read-only)."""
import csv
import json
import os

import pytest


pytestmark = pytest.mark.artifact  # требует локальные research-артефакты backend/reports (см. аудит P0.4)


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "entry_confluence.json")) as f:
        return json.load(f)


def test_part_a_quorum_reconciles(report: dict) -> None:
    a = report["part_a_executed_by_quorum"]
    total = a["2"]["count"] + a["3"]["count"] + a["4+"]["count"]
    with open(os.path.join(RUN_DIR, "entry_intents.csv"), newline="") as f:
        executed = sum(1 for r in csv.DictReader(f) if r["engine_decision"] == "executed")
    assert total == executed, f"{total} != executed {executed}"


def test_quorum_from_mask_independent() -> None:
    """quorum_count <= len(functions_mask) для всех executed (независимый пересчёт)."""
    import ast
    with open(os.path.join(RUN_DIR, "entry_intents.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["engine_decision"] != "executed":
                continue
            mask = ast.literal_eval(r["functions_mask"])
            qc = int(r["quorum_count"] or 0)
            assert qc <= len(mask), f"{r['intent_id']}: qc={qc} > mask={len(mask)}"


def test_part_b_uses_only_rejected(report: dict) -> None:
    """Part B построен по rejected intents (не executed)."""
    pb = report["part_b_opportunity_cost"]
    # есть rejected_1_2_signals_by_reason
    assert "rejected_1_2_signals_by_reason" in pb
    with open(os.path.join(RUN_DIR, "entry_intents.csv"), newline="") as f:
        n_rejected = sum(1 for r in csv.DictReader(f) if r["engine_decision"] == "rejected")
    n_from_partb = sum(v.get("intents", 0) for k, v in pb.items() if isinstance(v, dict) and "intents" in v)
    # part B покрывает все rejected (по числу сигналов)
    assert n_from_partb == n_rejected, f"{n_from_partb} != rejected {n_rejected}"


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
                    if "entry_confluence" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
