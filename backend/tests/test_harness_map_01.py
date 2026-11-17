"""HARN-MAP-01: страж карты. Не расширяет матрицу, а ловит молчаливое расхождение.

Если кто-то добавит гейт в реестр, но не в цепочку, или наоборот — тест скажет.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.bot.gates import (  # noqa: E402
    GATES,
    MARKET_GATES,
    PORTFOLIO_GATES,
    TIME_GATES,
    TREND_GATES,
)

REPORT = BACKEND / "reports" / "harness_parity" / "runtime_gate_map_v1.json"


def load_report() -> dict:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_report_exists_and_covers_every_gate() -> None:
    report = load_report()
    keys = {g["key"] for g in report["gates"]}
    assert keys == {g.key for g in GATES}, "состав гейтов в отчёте разошёлся с реестром"


def test_every_chain_function_is_reachable_in_report() -> None:
    report = load_report()
    known = set()
    for row in report["gates"]:
        known.update(row["function"] or [])
    # функции, отдающие код, собранный на лету, учтены отдельным разделом
    dynamic = set(report.get("dynamic_reason_codes", {}))
    chain_funcs = {f.__name__ for f in TIME_GATES + MARKET_GATES + TREND_GATES
                   + PORTFOLIO_GATES}
    missing = chain_funcs - known - dynamic
    assert not missing, f"функции цепочек без строки в карте: {sorted(missing)}"


def test_dynamic_codes_are_declared_outside_registry() -> None:
    """gate_risk_day отдаёт risk_<state>: ключа нет в реестре, это должно быть видно."""
    report = load_report()
    dyn = report["dynamic_reason_codes"]
    assert "gate_risk_day" in dyn
    assert dyn["gate_risk_day"]["registry_keys"] == []
    assert any(d["id"] == "DIV-07" for d in report["divergences"])


def test_declared_only_keys_are_really_silent() -> None:
    """Коды, объявленные без внедрения, не должны начать отдаваться незаметно."""
    report = load_report()
    silent = {g["key"] for g in report["gates"]
              if g["implementation"] == "declared_only"}
    assert silent == {"budget", "policy_reject", "ai_watch", "bad_candle",
                      "stale_candle"}, (
        f"набор молчащих кодов изменился: {sorted(silent)} — обновите карту и DIV-02")


def test_divergences_and_contract_are_non_empty() -> None:
    report = load_report()
    assert len(report["divergences"]) >= 6
    musts = {c["must"] for c in report["unsupported_contract"]}
    assert "refuse" in musts, "контракт обязан требовать явного отказа"


def test_generator_never_writes_outside_its_report() -> None:
    """Генератор карты пишет единственный файл — отчёт; в app/ он только читает."""
    src = (BACKEND / "scripts" / "research" / "harness_map_01.py").read_text(
        encoding="utf-8")
    assert "setattr(" not in src, "карта не должна менять объекты конфигурации"
    assert "save_gates_config" not in src
    assert "apply_gates_config" not in src
    write_calls = [ln.strip() for ln in src.splitlines()
                   if ".write_text(" in ln or "open(" in ln]
    assert len(write_calls) == 1, f"ожидалась одна запись (отчёт): {write_calls}"
    assert "OUT_FILE" in write_calls[0]
    assert (BACKEND / "app").is_dir(), "генератор обязан читать код проекта"
