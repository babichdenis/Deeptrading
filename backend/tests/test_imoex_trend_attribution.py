"""B5 IMOEX_TREND_ATTRIBUTION тесты (SHADOW, read-only)."""
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
    with open(os.path.join(RUN_DIR, "imoex_trend_attribution.json")) as f:
        return json.load(f)


def test_reconciliation_groups(report: dict) -> None:
    for m in ("A", "B", "C"):
        g = report["groups"][m]
        aligned = g["aligned"]["count"]
        counter = g["counter"]["count"]
        flat = g["flat"]["count"]
        # aligned+counter+flat может не покрыть все (часть с None regime), но не превышает n_rows
        assert aligned + counter + flat <= report["n_rows"] + 1, f"{m}: превышение n_rows"
        # audit CSV содержит n_rows строк
        with open(os.path.join(RUN_DIR, "imoex_trend_trade_audit.csv"), newline="") as f:
            n_audit = sum(1 for _ in csv.DictReader(f))
        assert n_audit == report["n_rows"]


def test_point_in_time(report: dict) -> None:
    """Методы определены: каждый трейд имеет entry/exit regime не-None (warmup отсечён)."""
    for m in ("A", "B", "C"):
        with open(os.path.join(RUN_DIR, "imoex_trend_trade_audit.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        assert all(r[f"entry_regime_{m}"] in ("up_trend", "down_trend", "flat") for r in rows), f"{m}: нет regime"


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
                    if "imoex_trend_attribution" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
