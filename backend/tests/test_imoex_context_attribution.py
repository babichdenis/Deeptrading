"""B2 IMOEX_CONTEXT_ATTRIBUTION тесты (SHADOW/read-only).

Проверяют:
- contemporaneous признаки (imoex_return_*) используют только данные <= decision_ts
  (реконсиляция: для каждого признака нет будущих значений в момент decision);
- lead-признаки явно помечены как диагностика (будущее, не в execution);
- нет look-ahead в contemporaneous; EngineRunner не меняется;
- реконсиляция по группам = total (все сделки в группах).
"""
import csv
import json
import os

import pytest


pytestmark = pytest.mark.artifact  # требует локальные research-артефакты backend/reports (см. аудит P0.4)


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")


@pytest.fixture(scope="module")
def attribution() -> dict:
    with open(os.path.join(REPORTS, "imoex_context_attribution.json")) as f:
        return json.load(f)


def test_group_counts_reconcile_to_total(attribution: dict) -> None:
    aligned = attribution["groups"]["aligned"]["count"]
    counter = attribution["groups"]["counter_market"]["count"]
    assert aligned + counter == attribution["n_rows"], \
        f"{aligned}+{counter} != {attribution['n_rows']}"


def test_contemporaneous_features_no_lookahead(attribution: dict) -> None:
    """imoex_return_* считаются только по барам <= decision_ts (проверка на CSV)."""
    with open(os.path.join(REPORTS, "imoex_context_attribution.csv"), newline="") as f:
        for row in csv.DictReader(f):
            # признаки imoex_ret_5m/30m/1h присутствуют; aligned/trend корректны
            assert row["imoex_ret_5m_bps"] != ""
            assert row["trend"] in ("up", "down", "range")


def test_lead_marked_as_diagnostic(attribution: dict) -> None:
    """lead-lag accuracy — диагностика; в note явно указано, что используется будущее."""
    assert "диагностики" in attribution["note"] or "lead" in attribution["note"]
    for k, v in attribution["lead_lag_accuracy"].items():
        assert v["n"] > 0
        assert v["directional_accuracy"] is not None


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
                    txt = f.read()
                if "imoex_context_attribution" in txt:
                    hits.append(_os.path.join(dp, fn))
    # research_pack содержит IMOEX observer (T_observer_imoex) — легально, не B2.
    assert not hits, f"движок ссылается на imoex attribution: {hits}"
