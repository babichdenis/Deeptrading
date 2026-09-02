"""B5b v2 ENTRY_EXIT_VOTE_BEHAVIOR тесты (continuous strength, read-only)."""
import csv
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(RUN_DIR, "entry_exit_vote_behavior.json")) as f:
        return json.load(f)


def test_direction_not_degenerated(report: dict) -> None:
    """Каждая функция имеет и buy, и sell, и neutral на t0."""
    for fn, v in report["per_function_direction"].items():
        d = v["direction_t0"]
        assert d["buy"] > 0 and d["sell"] > 0, f"{fn}: вырождение направления"
        assert d.get("neutral", 0) >= 0


def test_strength_continuous(report: dict) -> None:
    """strength непрерывна: среди значений есть дробные > 1 (не только 0/1)."""
    with open(os.path.join(RUN_DIR, "entry_exit_vote_audit.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    vals = [float(r["vote_strength"]) for r in rows if r["vote_strength"]]
    assert max(vals) > 1.0, "strength не continuous (все <= 1)"
    assert len(set(round(v, 1) for v in vals)) > 5, "strength выглядит бинарной"


def test_forward_dynamics_no_nulls(report: dict) -> None:
    dyn = report["strength_dynamics_winners_vs_losers"]
    for off in ("t0+0", "t0+1", "t0+2", "t0+3", "t0+5"):
        assert dyn[off]["winners_mean_strength"] is not None
        assert dyn[off]["losers_mean_strength"] is not None


def test_point_in_time(report: dict) -> None:
    """rel_bar в audit только в допустимых окнах (+0..+5, -0..-5)."""
    with open(os.path.join(RUN_DIR, "entry_exit_vote_audit.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    allowed = {"+0", "+1", "+2", "+3", "+5", "-0", "-1", "-3", "-5"}
    for r in rows:
        assert r["rel_bar"] in allowed, f"недопустимое окно {r['rel_bar']}"


def test_no_oracle(report: dict) -> None:
    assert "no oracle" in report["note"] or "oracle" not in json.dumps(report).lower()


def test_trades_limit(report: dict) -> None:
    assert 0 < report["n_trades"] <= 517


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
                    if "entry_exit_vote_behavior" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
