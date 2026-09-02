"""EXP-002b primary тесты (данные immutable из EXP-002 DRAFT, движок не перезапускался)."""
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "exp002b_primary_202508_202512.json")) as f:
        return json.load(f)


def test_config_immutable(report: dict) -> None:
    assert report["config_hash"] == "1c7f75dc44c2aa67"
    assert report["window"] == "2025-08-01..2025-12-31"


def test_criteria_present(report: dict) -> None:
    m = report["metrics"]
    for key in ("net_gte_090", "dd_lte_080", "pf_gte", "win_gte",
                "coverage_gte_35", "conc_lte_50"):
        assert key in m
    # все 6 критериев булевы
    assert sum(1 for k in ("net_gte_090", "dd_lte_080", "pf_gte", "win_gte",
                           "coverage_gte_35", "conc_lte_50") if m[k]) in (0, 1, 2, 3, 4, 5, 6)


def test_rejects_on_net_only(report: dict) -> None:
    """passed=false только из-за net; остальные 5 критериев True (диагностика)."""
    m = report["metrics"]
    assert m["dd_lte_080"] is True
    assert m["pf_gte"] is True
    assert m["win_gte"] is True
    assert m["coverage_gte_35"] is True
    assert m["conc_lte_50"] is True
    assert m["net_gte_090"] is False
    assert report["passed"] is False


def test_review_md_exists() -> None:
    assert os.path.exists(os.path.join(REPORTS, "exp002b_primary_202508_202512_MY3_review.md"))
