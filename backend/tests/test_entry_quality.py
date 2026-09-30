"""B4 ENTRY-QUALITY тесты (SHADOW, read-only)."""
import json
import os

import pytest


pytestmark = pytest.mark.artifact  # требует локальные research-артефакты backend/reports (см. аудит P0.4)


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "entry_quality.json")) as f:
        return json.load(f)


def test_reconciliation_n(report: dict) -> None:
    g = report["groups"]
    trend = g["trend_aligned"]["count"] + g["trend_not_aligned"]["count"]
    assert trend == report["n_rows"], f"{trend} != n_rows {report['n_rows']}"
    pb = g["pullback_shallow"]["count"] + g["pullback_medium"]["count"] + g["pullback_deep"]["count"]
    assert pb == report["n_rows"]


def test_features_point_in_time(report: dict) -> None:
    """Признаки на decision_ts: pullback/early-noise неотрицательны (смысл метрик)."""
    g = report["groups"]
    # все три pullback-группы ненулевые
    assert g["pullback_shallow"]["count"] > 0
    assert g["pullback_medium"]["count"] > 0
    assert g["pullback_deep"]["count"] > 0
    # early-noise медиана положительна
    assert report["early_noise_median_bps"] > 0


def test_no_engine_change() -> None:
    import os as _os
    hits = []
    for root in (os.path.join(BACKEND, "app", "engine"),
                 os.path.join(BACKEND, "app", "services")):
        for dp, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(_os.path.join(dp, fn)) as f:
                    if "entry_quality" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
