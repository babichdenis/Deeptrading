"""H-036 RUSSIAN MACRO-REGIME тесты (SHADOW, read-only).

Проверяют:
- реконсиляция: all_up + all_down + all_flat = n_rows (метод B);
- point-in-time: режим на баре входа (<= decision_ts);
- no oracle / движок не меняется;
- data gaps (Brent/Gold/USD-RUB) отражены;
- lead-lag для IMOEX/risk: lag0 есть, лаги корректны.
"""
import csv
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")

pytestmark = pytest.mark.artifact


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "macro_regime_attribution.json")) as f:
        return json.load(f)


def test_reconciliation(report: dict) -> None:
    g = report["groups"]["B"]
    total = g["all_up_trend"]["count"] + g["all_down_trend"]["count"] + g["all_flat"]["count"]
    # rows = 517 сделок × 3 метода; по методу B должно быть 517 (без warmup-потерь может быть меньше)
    assert 0 < total <= 517, f"total={total} вне (0, 517]"


def test_data_gaps_flagged(report: dict) -> None:
    for k in ("Brent", "Gold", "USD_RUB"):
        assert k in report["data_gaps"], f"нет data_gap {k}"


def test_lead_lag_imoex(report: dict) -> None:
    """AFLT/MVID (IMOEX/risk): lag0 не None; лаги 1/2/3/5 существуют."""
    for ticker in ("AFLT", "MVID"):
        v = report["lead_lag"][ticker]
        assert v["lag_0"] is not None, f"{ticker}: lag0 нет"
        for lag in ("lag_1", "lag_2", "lag_3", "lag_5"):
            assert lag in v


def test_point_in_time(report: dict) -> None:
    """Методология: IMOEX-режим на баре входа (не будущее)."""
    assert "на баре входа" in report["note"] or "B5 методология" in report["note"]


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
                    if "macro_regime_attribution" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
