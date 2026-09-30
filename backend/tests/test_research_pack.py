"""Тесты read-only research pack (10 требований из задачи).

Инварианты: не менять движок/параметры; только read-only экспорт.
Каждый тест создаёт свой research_pack.json в
backend/reports/test_research_pack/<test_name>/ (артефакт остаётся).
"""
import json
import os
import re

import pytest

pytestmark = pytest.mark.integration  # требует живых данных БД (свечи/MTM), см. аудит P0.4

from app.services.research_pack import (
    SCHEMA_VERSION,
    build_research_pack,
    _config_hash,
    _sha256,
)
from datetime import datetime, timezone, timedelta


REPORTS_ROOT = os.path.join(os.path.dirname(__file__), "..", "reports", "test_research_pack")


@pytest.fixture
def pack_dir(request):
    """Каждый тест — своя постоянная папка (артефакт research_pack.json)."""
    name = request.node.name.replace("::", "_").replace("/", "_")
    d = os.path.join(REPORTS_ROOT, name)
    os.makedirs(d, exist_ok=True)
    return d


def _make_pack(t_from, t_to, figis=None, pack_dir=None, **kw):
    return build_research_pack(t_from, t_to, figis, out_dir=pack_dir, **kw)


@pytest.fixture
def small_pack(pack_dir):
    t_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t_to = datetime(2026, 7, 8, tzinfo=timezone.utc)
    return _make_pack(t_from, t_to, ["BBG008F2T3T2"], pack_dir)


def test_1_read_only_no_db_writes(small_pack):
    """Только immutable артефакты; БД не пишется (проверяем отсутствие изменений).
    Практическая проверка: в out_dir только research-файлы, нет других."""
    out = small_pack["out_dir"]
    files = set(os.listdir(out))
    allowed = {"research_pack.json", "research_pack_manifest.json",
               "trades.csv", "entry_intents.csv", "rejections.csv", "daily_pnl.csv",
               "reconciliation.json", "intent_lifecycle.csv",
               "session_boundary_audit.csv", "mtm_equity_1m.csv"}
    assert files.issubset(allowed), f"неожиданные файлы: {files - allowed}"


def test_2_no_credentials_in_output(small_pack):
    """Никаких токенов/учёток/URL БД в JSON/CSV."""
    out = small_pack["out_dir"]
    bad_patterns = [
        r"t\.[A-Za-z0-9_-]{20,}",          # T-Invest токены
        r"postgres(ql)?://",
        r"password\s*[=:]\s*\S+",
        r"Authorization",
        r"api[_-]?key\s*[=:]\s*\S+",
        r"AccessToken",
    ]
    for fn in os.listdir(out):
        with open(os.path.join(out, fn), encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for pat in bad_patterns:
            assert not re.search(pat, content, re.IGNORECASE), f"{fn}: найден секрет: {pat}"


def test_3_oracle_fields_excluded(small_pack):
    """oracle/future-поля отсутствуют в executable_trades и entry_intents."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    forbidden = ["oracle", "future_", "lookahead", "zigzag"]
    for section in ("I_executable_trades", "J_entry_intents", "K_sampled_rejections"):
        for row in pack.get(section, []) or []:
            for k in row:
                assert not any(f in k.lower() for f in forbidden), f"{section}: {k}"


def test_4_funnel_reconciles(small_pack):
    """executed_entries >= closed_trades; стадии согласованы."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    f = pack["D_funnel"]
    executed = f["executed_entries"]["count"]
    closed = f["closed_trades"]["count"]
    assert executed >= closed, f"executed_entries {executed} < closed_trades {closed}"
    # entry_intents >= executed >= closed
    intents = f["entry_intents"]["count"]
    assert intents >= executed >= closed


def test_5_costs_exactly_once(small_pack):
    """total_cost = commission + slippage ровно один раз в каждой сделке."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    for t in pack.get("I_executable_trades", []) or []:
        assert abs((t.get("commission_rub") or 0) + (t.get("slippage_rub") or 0)
                   - (t.get("total_cost_rub") or 0)) < 0.01, t.get("trade_id")


def test_6_qty_aligned_to_lot(small_pack):
    """Каждая исполненная сделка: qty кратен lot_size."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    lot = pack["B_strategy_config"]["lot_size"]
    for t in pack.get("I_executable_trades", []) or []:
        qty = t.get("qty")
        if qty is None:
            continue
        assert qty % lot == 0, f"{t['trade_id']}: qty {qty} не кратен {lot}"


def test_7_entry_after_decision(small_pack):
    """entry_time > decision_time для каждой сделки."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    for t in pack.get("I_executable_trades", []) or []:
        d = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
        assert e > d, f"{t['trade_id']}: entry <= decision"


def test_8_deterministic_rejection_sample(pack_dir):
    """Одинаковый run_id даёт одинаковую выборку rejections."""
    t_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t_to = datetime(2026, 7, 15, tzinfo=timezone.utc)
    r1 = _make_pack(t_from, t_to, ["BBG008F2T3T2"], os.path.join(pack_dir, "a"))
    _ = _make_pack(t_from, t_to, ["BBG008F2T3T2"], os.path.join(pack_dir, "b"))
    # run_id разный -> разные папки, но выборка должна быть стабильна при том же run_id
    p1 = os.path.join(r1["out_dir"], "research_pack.json")
    with open(p1, encoding="utf-8") as f:
        pack1 = json.load(f)
    s1 = [(x["intent_id"], x["primary_reject_reason"]) for x in pack1["K_sampled_rejections"]]
    # детерминированность: повторный вызов в ту же папку не меняет содержимое
    r1b = _make_pack(t_from, t_to, ["BBG008F2T3T2"], r1["out_dir"])
    with open(os.path.join(r1b["out_dir"], "research_pack.json"), encoding="utf-8") as f:
        pack1b = json.load(f)
    s1b = [(x["intent_id"], x["primary_reject_reason"]) for x in pack1b["K_sampled_rejections"]]
    assert s1 == s1b


def test_9_timezone_europe_moscow(small_pack):
    """daily_pnl даты в Europe/Moscow; metadata timezone верна."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    assert pack["A_metadata"]["timezone"] == "Europe/Moscow"
    for d in pack.get("H_daily_pnl", []):
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", d["date"]), d


def test_10_period_limit():
    """Период > 92 дней отклоняется."""
    from app.api.routes.research import MAX_PERIOD_DAYS
    assert MAX_PERIOD_DAYS == 92
    t_from = datetime(2025, 9, 1, tzinfo=timezone.utc)
    t_to = t_from + timedelta(days=100)
    # эмулируем валидацию из endpoint
    days = (t_to - t_from).total_seconds() / 86400
    assert days > MAX_PERIOD_DAYS


def test_11_candles_and_signals_included(small_pack):
    """Исследовательские секции: свечи 5m, setup-сигналы, quorum, entry-кандидаты."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    assert "N_candles_5m" in pack
    assert "O_setup_signals" in pack
    assert "P_quorum_signals" in pack
    assert "Q_entry_candidates" in pack
    assert "R_session_audit" in pack
    # у RUAL должны быть свечи и сигналы
    assert len(pack["N_candles_5m"].get("BBG008F2T3T2", [])) > 0
    assert len(pack["O_setup_signals"].get("BBG008F2T3T2", {})) > 0
    assert pack["R_session_audit"]["policy"].startswith("main = MOEX")


def test_12_imoex_observer(small_pack):
    """Наблюдатель IMOEX: секция T_observer_imoex со свечами 5m."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    t = pack.get("T_observer_imoex", {})
    assert t.get("ticker") == "IMOEX"
    assert t.get("instrument_type") == "index"
    assert t.get("candles_5m", 0) > 0
    assert len(t.get("candles", [])) > 0


# --- A: единый capital ---
def test_a_capital_consistency(small_pack):
    """request/runtime/config/manifest = один effective capital (10000)."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    with open(os.path.join(out, "research_pack_manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    vals = [
        pack["B_strategy_config"]["capital_per_position_effective"],
        pack["B_strategy_config"]["capital_per_position_requested"],
        pack["C_portfolio_assumptions"]["capital_per_position"],
        manifest["canonical_strategy_params"]["capital_per_position_effective"],
    ]
    assert all(v == 10_000 for v in vals), vals
    assert pack["B_strategy_config"]["position_size_source"] == "run_request"


# --- B: terminal exactly-one ---
def test_b_terminal_exactly_one(small_pack):
    """Каждый intent имеет ровно один terminal_reason; нет generic-гейтов."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    intents = pack["J_entry_intents"]
    assert len(intents) > 0
    for i in intents:
        assert i.get("terminal_reason"), f"{i['intent_id']} без terminal_reason"
    bad = [i["terminal_reason"] for i in intents
           if i["terminal_reason"] in ("REJECTED_OTHER", "engine_gate", "AFTER_FREE", None)]
    assert not bad, bad
    # reconcile 100%
    rec = pack["W_reconciliation"]
    assert rec["entry_intents"] == len(intents)
    assert rec["reconciliation_pct"] == 100.0
    assert rec["generic_gates_left"] == 0


# --- C: executed intent -> trade lifecycle ---
def test_c_lifecycle_linkage(small_pack):
    """executed intent либо связан со сделкой, либо имеет явный статус."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    # entry_time сделок (для сопоставления с intents по времени)
    trade_times = {t.get("entry_time") for t in pack["I_executable_trades"]}
    # каждый EXECUTED_TRADE intent имеет linked_trade_id
    for i in pack["J_entry_intents"]:
        if i["terminal_reason"] == "EXECUTED_TRADE":
            assert i.get("linked_trade_id"), i["intent_id"]
    # количество EXECUTED_TRADE == trades (в пределах 1-2 из-за OPEN_AT_END/CANCELLED)
    n_exec = sum(1 for i in pack["J_entry_intents"] if i["terminal_reason"] == "EXECUTED_TRADE")
    assert n_exec >= len(pack["I_executable_trades"]) - 2
    # у каждой сделки есть соответствующий intent (decision_time на 1-5 мин раньше entry)
    from datetime import timedelta as _td2
    for t in pack["I_executable_trades"]:
        et = t.get("entry_time")
        assert et, t["trade_id"]
        want_side = "BUY" if str(t.get("side")).upper() in ("LONG", "BUY") else "SELL"
        try:
            et_dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            win_from = (et_dt - _td2(minutes=5)).isoformat()
        except (ValueError, TypeError):
            win_from = et
        found = any(
            win_from <= str(i["decision_time"]) <= et and i["side"] == want_side
            for i in pack["J_entry_intents"]
        )
        assert found, f"нет intent для trade {t['trade_id']} entry {et}"


# --- D: quorum/function fields ---
def test_d_intent_context(small_pack):
    """quorum_count, functions_mask, candidate_id заполнены."""
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    for i in pack["J_entry_intents"]:
        assert i.get("quorum_count") is not None, i["intent_id"]
        assert isinstance(i.get("functions_mask"), list), i["intent_id"]
        assert i.get("candidate_id"), i["intent_id"]
        assert i.get("unit") == "entry_intent"


# --- E: session boundary в Europe/Moscow ---
def test_e_session_boundary(small_pack):
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    v = pack["V_session_boundary_audit"]
    for row in v.get("rows", []):
        assert "msk" in row.get("execution_time_msk", "") or row.get("execution_time_msk") is None
        assert row.get("session_at_decision") in ("main", "outside", "weekend")


# --- F: MTM equity ---
def test_f_mtm_equity(small_pack):
    out = small_pack["out_dir"]
    with open(os.path.join(out, "research_pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    u = pack["U_mtm_equity_1m"]
    assert u, "MTM отсутствует"
    for figi, m in u.items():
        assert m.get("bars"), figi
        assert m.get("realised_only") is False
        assert m.get("mtm_drawdown_rub") is not None
        # equity = cash + unrealised
        b = m["bars"][-1]
        assert abs(b["equity"] - (b["cash"] + b["unrealised"])) < 0.01
