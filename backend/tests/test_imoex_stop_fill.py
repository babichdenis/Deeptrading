"""B2b-II IMOEX_STOP_FILL тесты (SHADOW counterfactual, read-only)."""
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")


@pytest.fixture(scope="module")
def report() -> dict:
    with open(os.path.join(REPORTS, "imoex_stop_fill.json")) as f:
        return json.load(f)


def test_no_lookahead(report: dict) -> None:
    assert "no look-ahead" in report["note"] or "IMOEX НЕ в execution" in report["note"]


def test_reconciliation(report: dict) -> None:
    """n сделок с path <= 517 (baseline July); threshold подмножества не пересекаются по trade_id."""
    results = report["results"]
    n10 = results["threshold_10"]["n_stop"]
    n20 = results["threshold_20"]["n_stop"]
    n30 = results["threshold_30"]["n_stop"]
    assert n10 > 0 and n20 > 0 and n30 > 0
    assert n10 >= n20 >= n30, "больший порог должен давать не больше стопов"
    assert n10 <= 517


def test_mae_after_reversal_negative(report: dict) -> None:
    """IMOEX-разворот опережает adverse-движение акции: median stock MAE после разворота < 0."""
    for thr in ("threshold_10", "threshold_20", "threshold_30"):
        v = report["results"][thr]["median_stock_mae_after_reversal_bps"]
        assert v is not None and v < 0, f"{thr}: MAE after reversal = {v} (ожидалось < 0)"


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
                    if "imoex_stop_fill" in f.read():
                        hits.append(_os.path.join(dp, fn))
    assert not hits
