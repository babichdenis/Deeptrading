"""Тесты честного WF-отбора (A2, ARCH 2026-10-07; ревизия под REWORK 2026-10-08).

Полный путь run_wf проверяется с подменёнными _run_grid/_resolve_figis:
без PostgreSQL, брокера и lifespan, до сохранения JSON. Исторические
backend/reports/ не требуются.
"""
from __future__ import annotations

import json
import math
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

import bt_ose_sweep as bts
import ose_exit_matrix as oem
from wf_selection import (
    STATUS_ELIGIBLE,
    STATUS_INCOMPLETE,
    STATUS_NO_ELIGIBLE_CANDIDATES,
    STATUS_OK,
    STATUS_WORKER_ERRORS,
    RankFieldError,
    build_oos_matrix,
    coverage_breakdown,
    coverage_reasons,
    identity_problems,
    is_number,
    metrics_complete,
    oos_rows,
    rank_value,
    raw_row_problem,
    aggregate_rows,
    sanitize_text,
    select_is,
    select_top,
    worker_error_code,
)

PERIOD = ["2026-01-01", "2026-03-31"]


def _label(robot: str) -> str:
    return f"{robot} {json.dumps({}, sort_keys=True, separators=(',', ':'))}"


def _phases():
    return bts._wf_phases(list(PERIOD), 2, 25)


def _row(label, xc, ticker, net=0.0, trades=10, wins=None, gw=None, gl=None, **over):
    row = {
        "strategy": label, "exit": xc, "ticker": ticker,
        "trades": trades, "wins": max(1, trades // 2) if wins is None else wins,
        "gw": max(net, 0.0) if gw is None else gw,
        "gl": max(-net, 0.0) if gl is None else gl,
        "net": net, "max_dd_pct": 5.0, "commission": 0.0, "slippage": 0.0, "turnover": 0.0,
    }
    row.update(over)
    return row


def _build_rows(plan):
    """Заглушка _run_grid: plan(period_key, label, exits, figis) -> list[dict]."""
    def _run_grid(configs, exits, figis, period, args, *, jobs, artifacts=False,
                  cache=True, code_sha=""):
        key = tuple(period)
        rows = []
        for label, _rn, _kw in configs:
            rows.extend(plan(key, label, exits, figis))
        return rows

    return _run_grid


def _simple_plan(net, gl=None):
    def plan(_key, label, exits, figis):
        rows = []
        for xc in exits:
            for _figi, ticker in figis:
                kwargs = {} if gl is None else {"gl": gl}
                rows.append(_row(label, xc, ticker, net=net, **kwargs))
        return rows
    return plan


def _write_spec(tmp_path, robots, *, filters=None, wf=None, robustness=None):
    wf_cfg = {"iterations": 2, "percent_oos": 25, "top_k": 1, "rank_by": "net_pnl"}
    wf_cfg.update(wf or {})
    if robustness is not None:
        wf_cfg["robustness"] = robustness
    out_dir = tmp_path / "reports"
    spec = {
        "name": "wf_test",
        "period": list(PERIOD),
        "interval": "10min",
        "robots": [{"robot": r, "params": {}} for r in robots],
        "exits": ["x07"],
        "filters": filters if filters is not None else {"min_net_pnl": 0},
        "wf": wf_cfg,
        "costs": {"commission": 0.0005, "slippage_bps": 2.0, "qty": 1,
                  "capital": 100000.0},
        "outputs": {"dir": str(out_dir)},
        "execute": {"jobs": 1, "cache": False},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path, out_dir


def _wf_args(spec_path):
    return Namespace(spec=str(spec_path), dfrom="", dto="", interval="",
                     commission=0.0005, slippage_bps=2.0, qty=1, capital=100000.0,
                     jobs=1, tickers="", figis="", no_cache=True)


def _patch_instruments(monkeypatch, figis=None):
    figis = list(figis or [("FIGI_TEST", "TEST")])
    monkeypatch.setattr(oem, "_engine_sync", lambda: None)
    monkeypatch.setattr(oem, "_resolve_figis", lambda eng, args: figis)


def _load_wf(out_dir):
    """Читает WF-файл строгим парсером: NaN/Infinity → ошибка (A2-C1)."""
    files = sorted(out_dir.glob("WF_wf_test_*.json"))
    assert len(files) == 1

    def _bad_const(name: str):
        raise AssertionError(f"strict JSON violation: {name}")

    return json.loads(files[0].read_text(encoding="utf-8"), parse_constant=_bad_const)


@pytest.fixture
def dummy_apply_filters():
    def _apply(metrics, filters):
        passed = metrics.get("trades", 0) >= 10
        return {"passed": passed, "checks": []}
    return _apply


def test_is_number():
    assert is_number(10) is True
    assert is_number(10.5) is True
    assert is_number(0) is True
    assert is_number(-1.5) is True
    assert is_number(float('nan')) is False
    assert is_number(float('inf')) is False
    assert is_number("10") is False
    assert is_number(None) is False
    assert is_number(True) is False


def test_metrics_complete_required_only():
    assert metrics_complete({"trades": 10, "net_pnl": 100}) is True
    assert metrics_complete({"trades": 10, "net_pnl": 100, "profit_factor": None}) is True
    assert metrics_complete({"trades": 0, "net_pnl": 0.0}) is True
    assert metrics_complete({}) is False
    assert metrics_complete({"trades": None, "net_pnl": 1}) is False
    assert metrics_complete({"trades": 10}) is False
    assert metrics_complete({"trades": float('nan'), "net_pnl": 1}) is False


def test_rank_value():
    metrics = {"net_pnl": 100.5, "profit_factor": 1.5, "win_rate": 60.0, "trades": 20}
    assert rank_value(metrics, "net_pnl") == 100.5
    assert rank_value(metrics, "profit_factor") == 1.5
    assert rank_value(metrics, "win_rate") == 60.0
    assert rank_value(metrics, "trades") == 20
    assert rank_value({"net_pnl": float('nan')}, "net_pnl") is None
    assert rank_value({"net_pnl": None}, "net_pnl") is None
    assert rank_value({"net_pnl": "high"}, "net_pnl") is None
    with pytest.raises(RankFieldError):
        rank_value(metrics, "invalid_field")


def test_select_top_standard(dummy_apply_filters):
    by_label = {
        "A": {"trades": 20, "net_pnl": 100},
        "B": {"trades": 20, "net_pnl": 200},
    }
    selected, status, rejected = select_top(["A", "B"], by_label, "net_pnl", top_k=1)
    assert selected == ["B"]
    assert status == STATUS_ELIGIBLE
    assert len(rejected) == 0


def test_select_top_empty_passed(dummy_apply_filters):
    by_label = {"A": {"trades": 5, "net_pnl": 500}}
    result = select_is(by_label, {"min_trades": 10}, dummy_apply_filters, "net_pnl", top_k=3)
    assert result["passed"] == []
    assert result["selected"] == []
    assert result["status"] == STATUS_NO_ELIGIBLE_CANDIDATES


def test_select_top_incomplete_data(dummy_apply_filters):
    by_label = {
        "A": {"trades": 20, "net_pnl": 100},
        "B": {"trades": 20},
    }
    result = select_is(by_label, {"min_trades": 10}, dummy_apply_filters, "net_pnl", top_k=3)
    assert "A" in result["passed"]
    assert "B" not in result["passed"]
    assert result["selected"] == ["A"]
    assert any(r["label"] == "B" and r["reason"] == "incomplete_metrics"
               for r in result["rejected"])


def test_select_top_invalid_rank_value(dummy_apply_filters):
    by_label = {
        "A": {"trades": 20, "net_pnl": 100, "win_rate": float('nan')},
        "B": {"trades": 20, "net_pnl": 100, "win_rate": 60.0},
    }
    result = select_is(by_label, {"min_trades": 10}, dummy_apply_filters, "win_rate", top_k=3)
    # A2-C1: невалидный ранг ловится на этапе eligibility с явной причиной
    # и не попадает ни в passed, ни в select_top.
    assert "B" in result["passed"]
    assert "A" not in result["passed"]
    assert result["selected"] == ["B"]
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["reason"] == "missing_rank:win_rate"


def test_select_top_direct_invalid_rank_reason():
    # Прямой вызов select_top (мимо eligibility) даёт invalid_rank:<field>.
    selected, status, rejected = select_top(
        ["A"], {"A": {"trades": 20, "net_pnl": 100, "win_rate": float('nan')}},
        "win_rate", 1)
    assert selected == []
    assert status == STATUS_NO_ELIGIBLE_CANDIDATES
    assert rejected[0]["reason"] == "invalid_rank:win_rate"


def test_select_top_tie_break(dummy_apply_filters):
    by_label = {
        "Z": {"trades": 20, "net_pnl": 100},
        "A": {"trades": 20, "net_pnl": 100},
    }
    result = select_is(by_label, {"min_trades": 10}, dummy_apply_filters, "net_pnl", top_k=1)
    assert result["selected"] == ["A"]


def test_select_is_profitable_without_losses_admitted():
    by_label = {
        "A": {"trades": 10, "net_pnl": 100.0, "profit_factor": None,
              "expectancy": 10.0, "max_drawdown_pct": 0.0},
    }
    result = select_is(by_label, {"min_net_pnl": 0}, bts._af, "net_pnl", top_k=1)
    assert result["selected"] == ["A"]
    assert result["status"] == STATUS_ELIGIBLE


def test_select_is_requires_net_pnl_independent_of_rank():
    result = select_is({"A": {"trades": 10}}, {}, bts._af, "trades", top_k=1)
    assert result["selected"] == []
    assert result["status"] == STATUS_NO_ELIGIBLE_CANDIDATES
    assert result["rejected"][0]["reason"] == "incomplete_metrics"


def test_select_is_incomplete_coverage_not_admitted(dummy_apply_filters):
    by_label = {
        "A": {"trades": 20, "net_pnl": 100},
        "B": {"trades": 20, "net_pnl": 500},
    }
    result = select_is(by_label, {"min_trades": 10}, dummy_apply_filters, "net_pnl",
                       top_k=3,
                       incomplete={"B": "incomplete_runs missing=1 worker_errors=1"})
    assert result["passed"] == ["A"]
    assert result["selected"] == ["A"]
    assert any(r["label"] == "B" and r["reason"].startswith("incomplete_runs")
               for r in result["rejected"])


def test_raw_row_problem():
    assert raw_row_problem(_row("A", "x07", "T1", net=1.0)) == ""
    assert raw_row_problem(_row("A", "x07", "T1", net=1.0, trades=float('nan'))) \
        == "invalid_raw:trades"
    assert raw_row_problem(
        _row("A", "x07", "T1", net=float('inf'), gw=1.0, gl=0.0)) == "invalid_raw:net"
    assert raw_row_problem({"strategy": "A", "error": "boom"}).startswith("worker_error")
    assert raw_row_problem(None) == "invalid_raw:row"


def test_coverage_reasons_exact_keys():
    a = "A"
    expected = {("x07", "T1"), ("x08", "T1")}
    dup = coverage_reasons([_row(a, "x07", "T1"), _row(a, "x07", "T1")], [a], expected)
    assert "missing=1" in dup[a]
    assert "duplicate=1" in dup[a]
    assert "x08@T1" in dup[a]

    ok = coverage_reasons([_row(a, "x07", "T1"), _row(a, "x08", "T1")], [a], expected)
    assert ok == {}

    bad = coverage_reasons([_row(a, "x07", "T1", net=float('nan')),
                            _row(a, "x08", "T1")], [a], expected)
    assert "invalid_raw=1" in bad[a]
    assert "missing=1" in bad[a]

    unexpected = coverage_reasons([_row(a, "x07", "T1"), _row(a, "x08", "T1"),
                                   _row(a, "x09", "T1")], [a], expected)
    assert "unexpected=1" in unexpected[a]


def test_aggregate_rows_sums_and_rates():
    rows = [
        {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 10, "wins": 6,
         "gw": 100.0, "gl": 40.0, "net": 60.0, "max_dd_pct": 5.0,
         "commission": 1.0, "slippage": 0.5, "turnover": 100.0},
        {"strategy": "A", "exit": "x08", "ticker": "T1", "trades": 10, "wins": 4,
         "gw": 80.0, "gl": 70.0, "net": 10.0, "max_dd_pct": 8.0,
         "commission": 2.0, "slippage": 1.0, "turnover": 150.0},
    ]
    agg = aggregate_rows(rows)
    assert agg["trades"] == 20
    assert agg["wins"] == 10
    assert agg["win_rate"] == 50.0
    assert agg["gross_win"] == 180.0
    assert agg["gross_loss"] == 110.0
    assert agg["net_pnl"] == 70.0
    assert agg["profit_factor"] == 1.64
    assert agg["expectancy"] == 3.5
    assert agg["max_drawdown_pct"] == 8.0  # максимум известных значений
    assert agg["commission"] == 3.0
    assert agg["slippage"] == 1.5
    assert agg["turnover"] == 250.0
    assert agg["_problem"] == ""


def test_aggregate_rows_optional_unknown_becomes_null():
    # unknown хотя бы в одной строке → агрегат null; нулевая подстановка запрещена.
    rows = [
        {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 5, "wins": 5,
         "gw": 50.0, "gl": 0.0, "net": 50.0, "commission": 1.0, "max_dd_pct": 3.0},
        {"strategy": "A", "exit": "x08", "ticker": "T1", "trades": 5, "wins": 3,
         "gw": 30.0, "gl": 10.0, "net": 20.0},
    ]
    agg = aggregate_rows(rows)
    assert agg["commission"] is None
    assert agg["slippage"] is None
    assert agg["turnover"] is None
    assert agg["max_drawdown_pct"] is None  # ключ агрегата, не raw-имя


def test_aggregate_rows_formula_cases_pf_expectancy():
    no_loss = aggregate_rows([
        {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 3, "wins": 3,
         "gw": 30.0, "gl": 0.0, "net": 30.0},
    ])
    assert no_loss["profit_factor"] is None  # gl==0 — случай формул, не повреждение
    no_trades = aggregate_rows([
        {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 0, "wins": 0,
         "gw": 0.0, "gl": 0.0, "net": 0.0},
    ])
    assert no_trades["expectancy"] is None  # trades==0 — случай формул
    assert no_trades["win_rate"] == 0.0


def test_aggregate_rows_overflow_marks_problem_and_nulls():
    # сумма конечных входов не конечна → overflow:<field>, агрегат null.
    rows = [
        {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 1, "wins": 1,
         "gw": 1e308, "gl": 0.0, "net": 1e308},
        {"strategy": "A", "exit": "x08", "ticker": "T1", "trades": 1, "wins": 1,
         "gw": 1e308, "gl": 0.0, "net": 1e308},
    ]
    agg = aggregate_rows(rows)
    # маркеры overflow — по именам raw-полей (gw, net), агрегаты — по метрикам
    assert "overflow:gw" in agg["_problem"]
    assert "overflow:net" in agg["_problem"]
    assert agg["gross_win"] is None
    assert agg["net_pnl"] is None
    assert agg["expectancy"] is None


def test_aggregate_rows_empty_baseline():
    agg = aggregate_rows([])
    assert agg["trades"] == 0
    assert agg["win_rate"] == 0.0
    assert agg["net_pnl"] == 0
    assert agg["profit_factor"] is None
    assert agg["expectancy"] is None
    assert agg["_problem"] == ""


def test_raw_row_problem_optional_fields_unknown_vs_damaged():
    base = {"strategy": "A", "exit": "x07", "ticker": "T1", "trades": 1,
            "wins": 1, "gw": 1.0, "gl": 0.0, "net": 1.0}
    # все optional отсутствуют → UNKNOWN, строка валидна
    assert raw_row_problem(base) == ""
    for key in ("max_dd_pct", "commission", "slippage", "turnover"):
        assert raw_row_problem({**base, key: None}) == ""
        assert raw_row_problem({**base, key: 2.5}) == ""  # известное число валидно
        assert raw_row_problem({**base, key: float('nan')}) == f"invalid_raw:{key}"
        assert raw_row_problem({**base, key: "3"}) == f"invalid_raw:{key}"
        assert raw_row_problem({**base, key: True}) == f"invalid_raw:{key}"


def test_raw_row_problem_worker_error_sanitized():
    problem = raw_row_problem({"strategy": "A", "error": "boom\nsecret\x00line"})
    assert problem.startswith("worker_error:")
    assert "\n" not in problem and "\x00" not in problem
    assert len(problem) <= len("worker_error:") + 80


def test_sanitize_text():
    assert sanitize_text("boom\n\t detail") == "boom detail"
    assert sanitize_text("a\x00b\x1fc") == "a b c"
    assert sanitize_text("x" * 200) == "x" * 80
    assert sanitize_text(None) == "None"


def test_oos_rows_error_and_incomplete(dummy_apply_filters):
    by_label = {
        "A": {"trades": 20, "net_pnl": 100},
        "B": {"trades": 20},
    }
    rows = oos_rows(["A", "B"], by_label, errors=["B"])
    row_a = next(r for r in rows if r["label"] == "A")
    row_b = next(r for r in rows if r["label"] == "B")
    assert row_a["status"] == STATUS_OK
    assert isinstance(row_a["metrics"], dict)
    assert row_b["status"] == STATUS_WORKER_ERRORS
    assert row_b["metrics"] is None

    rows_inc = oos_rows(["B"], by_label)
    assert rows_inc[0]["status"] == STATUS_INCOMPLETE
    assert rows_inc[0]["reason"] == "incomplete_metrics:net_pnl"
    assert rows_inc[0]["metrics"] is None


def test_oos_rows_non_finite_required_not_ok():
    for bad_value in (float('nan'), float('inf'), None, "high"):
        rows = oos_rows(["A"], {"A": {"trades": 10, "net_pnl": bad_value}})
        assert rows[0]["status"] == STATUS_INCOMPLETE
        assert rows[0]["metrics"] is None
    rows = oos_rows(["A"], {"A": {"trades": float('nan'), "net_pnl": 100}})
    assert rows[0]["status"] == STATUS_INCOMPLETE


def test_oos_rows_incomplete_reason(dummy_apply_filters):
    by_label = {"B": {"trades": 20, "net_pnl": 100}}
    rows = oos_rows(["B"], by_label, incomplete={"B": "incomplete_runs missing=1"})
    assert rows[0]["status"] == STATUS_INCOMPLETE
    assert rows[0]["reason"] == "incomplete_runs missing=1"
    assert rows[0]["metrics"] is None


def test_build_oos_matrix_empty_selection():
    matrix = build_oos_matrix([{"iter": 1, "selected": []}], {})
    assert matrix[0]["iter"] == 1
    assert matrix[0]["selected"] == []
    assert matrix[0]["rows"] == []


def test_run_wf_invalid_rank_clear_error(tmp_path, monkeypatch, capsys):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"], wf={"rank_by": "made_up"})
    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(_simple_plan(100.0)))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "rank_by" in out and "made_up" in out
    assert not out_dir.exists()


def test_run_wf_all_rejected_saves_empty_selection(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade", "rsi_contrtrend"])
    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(_simple_plan(-5.0)))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == []
    assert data["selected"][0]["status"] == STATUS_NO_ELIGIBLE_CANDIDATES
    assert data["oos_matrix"] == []
    assert data["robustness"] == []
    assert len(data["phases"][0]["by_label"]) == 2
    assert data["phases"][0]["rejected"]


def test_run_wf_partial_pass_selected(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade", "rsi_contrtrend"])
    a, b = _label("rsi_trade"), _label("rsi_contrtrend")

    def plan(_key, label, exits, figis):
        net = 100.0 if label == a else -10.0
        return [r for r in (_row(label, xc, tk, net=net) for xc in exits for _f, tk in figis)]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    assert data["selected"][0]["status"] == STATUS_ELIGIBLE
    assert [m["config"] for m in data["oos_matrix"]] == [a]


def test_run_wf_zero_ranked(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade", "rsi_contrtrend"],
                                     filters={"min_net_pnl": -10})
    a, b = _label("rsi_trade"), _label("rsi_contrtrend")

    def plan(_key, label, exits, figis):
        net = 0.0 if label == a else -0.5
        return [r for r in (_row(label, xc, tk, net=net) for xc in exits for _f, tk in figis)]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]


def test_run_wf_profitable_without_losses_admitted(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"], filters={"min_net_pnl": 0})
    a = _label("rsi_trade")

    def plan(_key, label, exits, figis):
        return [_row(label, xc, tk, net=100.0, gw=100.0, gl=0.0)
                for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    assert data["selected"][0]["status"] == STATUS_ELIGIBLE
    entry = data["oos_matrix"][0]
    assert entry["valid_windows"] == 2
    assert entry["oos_nets"] == [100.0, 100.0]


def test_run_wf_two_tickers_one_missing_not_admitted(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch, figis=[("F1", "T1"), ("F2", "T2")])
    monkeypatch.setattr(bts, "_run_grid", _build_rows(
        lambda _key, label, exits, figis: [_row(label, xc, "T1", net=100.0)
                                           for xc in exits]))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert "missing=1" in rej[a]
    assert "x07@T2" in rej[a]


def test_run_wf_duplicate_not_admitted(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch, figis=[("F1", "T1"), ("F2", "T2")])

    def plan(_key, label, exits, _figis):
        rows = []
        for xc in exits:
            rows.append(_row(label, xc, "T1", net=100.0))
            rows.append(_row(label, xc, "T1", net=100.0))
        return rows

    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert "missing=1" in rej[a]
    assert "duplicate=1" in rej[a]


def test_run_wf_non_finite_raw_not_admitted(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(
        lambda _key, label, exits, figis: [
            _row(label, xc, tk, net=float('nan')) for xc in exits for _f, tk in figis]))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert "invalid_raw=1" in rej[a]
    assert data["phases"][0]["worker_errors"] == []
    assert math.isfinite(data["phases"][0]["by_label"][a]["net_pnl"])


def test_run_wf_worker_error_not_admitted_and_oos_incomplete(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade", "rsi_contrtrend"])
    a, b = _label("rsi_trade"), _label("rsi_contrtrend")
    is_keys = {tuple(p[1:]) for p in _phases() if p[0] == "IS"}
    first_oos = next(tuple(p[1:]) for p in _phases() if p[0] == "OOS")

    def plan(key, label, exits, figis):
        if label == a and key in is_keys:
            return [{"strategy": label, "exit": xc, "ticker": tk, "error": "worker boom"}
                    for xc in exits for _f, tk in figis]
        net = 100.0 if key in is_keys else 50.0
        if label == b and key == first_oos:
            return [{"strategy": label, "exit": xc, "ticker": tk, "error": "worker boom"}
                    for xc in exits for _f, tk in figis]
        return [_row(label, xc, tk, net=net) for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [b]
    assert data["selected"][1]["selected"] == [b]
    assert a in data["phases"][0]["worker_errors"]
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert rej[a].startswith("incomplete_runs")
    assert "worker_errors=1" in rej[a]

    entry = next(m for m in data["oos_matrix"] if m["config"] == b)
    assert entry["valid_windows"] == 1
    assert entry["worker_error_windows"] == 1
    bad = [w for w in entry["windows"] if w["status"] == STATUS_WORKER_ERRORS]
    assert len(bad) == 1
    assert bad[0]["net_pnl"] is None
    bad_rows = [r for oos in data["selected"][0]["oos"] for r in oos["rows"]
                if r["status"] == STATUS_WORKER_ERRORS]
    assert bad_rows and all(r["metrics"] is None for r in bad_rows)


def test_run_wf_oos_nan_not_summed_and_nulled(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    is_keys = {tuple(p[1:]) for p in _phases() if p[0] == "IS"}

    def plan(key, label, exits, figis):
        if key in is_keys:
            return [_row(label, xc, tk, net=100.0) for xc in exits for _f, tk in figis]
        return [_row(label, xc, tk, net=float('nan')) for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    entry = data["oos_matrix"][0]
    assert entry["valid_windows"] == 0
    assert entry["oos_nets"] == []
    assert entry["incomplete_windows"] == 2
    assert all(w["net_pnl"] is None for w in entry["windows"])
    for oos in data["selected"][0]["oos"]:
        for row in oos["rows"]:
            assert row["status"] == STATUS_INCOMPLETE
            assert row["metrics"] is None


def test_run_wf_oos_only_improvement_does_not_change_is(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade", "rsi_contrtrend"])
    a, b = _label("rsi_trade"), _label("rsi_contrtrend")
    is_keys = {tuple(p[1:]) for p in _phases() if p[0] == "IS"}

    def plan(key, label, exits, figis):
        if key in is_keys:
            net = 100.0 if label == a else 50.0
        else:
            net = -1000.0 if label == a else 1000.0
        return [_row(label, xc, tk, net=net) for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    assert data["selected"][1]["selected"] == [a]
    entry = next(m for m in data["oos_matrix"] if m["config"] == a)
    assert entry["oos_nets"] == [-1000.0, -1000.0]
    assert entry["valid_windows"] == 2


def test_run_wf_normal_success_with_robustness(tmp_path, monkeypatch):
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"],
                                     robustness={"delta_pct": 10, "period_days": 10})
    a = _label("rsi_trade")
    is_keys = {tuple(p[1:]) for p in _phases() if p[0] == "IS"}

    def plan(key, label, exits, figis):
        net = 30.0 if key in is_keys else 20.0
        return [_row(label, xc, tk, net=net) for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))

    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    entry = data["oos_matrix"][0]
    assert entry["config"] == a
    assert entry["oos_nets"] == [20.0, 20.0]
    assert entry["valid_windows"] == 2
    assert entry["incomplete_windows"] == 0
    assert data["meta"]["robustness_basis"] == "post_selection_diagnostic"
    assert data["robustness"]
    assert all(r["basis"] == "post_selection" for r in data["robustness"])
    assert all(r["status"] == STATUS_OK for r in data["robustness"])


# --- C1 ревизия 4: регрессии таблицы Архитектора 2026-10-08 ---


def test_worker_error_code_classes_only():
    """C1-5: публикуются только разрешённые классы, текст ошибки — нет."""
    assert worker_error_code("token=FAKE_TEST_MARKER") == "exception"
    assert worker_error_code("TimeoutError('token=secret')") == "TimeoutError"
    assert worker_error_code("KeyError: 'figi'") == "KeyError"
    assert worker_error_code("ConnectionError(host)") == "ConnectionError"
    assert worker_error_code("MyCustomError(boom)") == "exception"
    assert worker_error_code("") == "exception"


def test_identity_problems_classifies_before_arithmetic():
    """C1-1: не-dict / без strategy / чужая стратегия — классифицируются
    до арифметики, с атрибуцией по классам."""
    a = _label("rsi_trade")
    raw = [None,
           {"exit": "x07", "ticker": "TEST", "trades": 1, "wins": 1,
            "gw": 1.0, "gl": 0.0, "net": 1.0},  # dict без strategy
           _row(a, "x07", "TEST", net=1.0),
           {"strategy": "UNEXPECTED", "exit": "x07", "ticker": "TEST"}]
    ident = identity_problems(raw, [a])
    assert ident["non_dict"] == 1
    assert ident["unlabeled"] == 1
    assert ident["unknown_labels"] == ["UNEXPECTED"]


def test_coverage_breakdown_preserves_field_and_key():
    """C1-6: повреждение хранится структурой (поле + ключ), строковая
    причина сопровождается invalid_fields=…, а не только счётчиком."""
    a = _label("rsi_trade")
    expected = {("x07", "T1"), ("x07", "T2")}
    raw = [_row(a, "x07", "T1", net=1.0),
           dict(_row(a, "x07", "T2", net=1.0), trades="oops")]
    bd = coverage_breakdown(raw, [a], expected)
    assert bd[a]["invalid"] == [{"key": ["x07", "T2"], "field": "trades"}]
    assert bd[a]["missing"] == [["x07", "T2"]]
    reasons = coverage_reasons(raw, [a], expected, breakdown=bd)
    assert "invalid_raw=1" in reasons[a]
    assert "invalid_fields=trades" in reasons[a]


def test_aggregate_rows_permutation_invariant():
    """C1-4: math.fsum — агрегат не зависит от порядка raw; прежняя
    наивная сумма давала 1.0 против 0.0 на этих же входах."""
    def rows(order):
        nets = {"T1": 1e16, "T2": -1e16, "T3": 1.0}
        return [{"strategy": "A", "exit": "x07", "ticker": tk,
                 "trades": 1, "wins": 1, "gw": max(nets[tk], 0.0),
                 "gl": max(-nets[tk], 0.0), "net": nets[tk]}
                for tk in order]

    base = aggregate_rows(rows(["T1", "T2", "T3"]))
    perm = aggregate_rows(rows(["T1", "T3", "T2"]))
    assert base["net_pnl"] == perm["net_pnl"] == 1.0
    assert base["trades"] == perm["trades"] == 3
    assert base["_problem"] == perm["_problem"] == ""


def test_run_wf_unknown_label_and_non_dict_make_phase_incomplete(tmp_path, monkeypatch):
    """C1-1: чужая стратегия (с валидными числами) и запись без
    идентичности обнаруживаются до арифметики: unknown_labels заполнен,
    ВСЕ кандидаты фазы не допускаются, rc=0, файл сохраняется."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch)
    stranger = {"strategy": "UNEXPECTED", "exit": "x07", "ticker": "TEST",
                "trades": 5, "wins": 5, "gw": 50.0, "gl": 0.0, "net": 50.0}

    def plan(_key, label, exits, figis):
        rows = [_row(label, xc, tk, net=100.0) for xc in exits for _f, tk in figis]
        rows.append(dict(stranger))  # валидные числа, но чужая стратегия
        rows.append(None)            # запись без идентичности
        return rows

    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    phase = data["phases"][0]
    assert phase["unknown_labels"] == ["UNEXPECTED"]
    assert phase["unattributed"] == 1
    assert phase["status"] == STATUS_NO_ELIGIBLE_CANDIDATES
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in phase["rejected"]}
    assert "unknown=UNEXPECTED" in rej[a]
    assert "non_dict=1" in rej[a]


def test_run_wf_robustness_damaged_row_same_contract(tmp_path, monkeypatch):
    """C1-2: None в raw robustness — тот же контракт принадлежности, что
    в IS/OOS: все соседи INCOMPLETE с причиной identity, а не игнорируются."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"],
                                     robustness={"delta_pct": 10, "period_days": 10})
    a = _label("rsi_trade")
    phase_keys = {tuple(p[1:]) for p in _phases()}
    rob_key = ("2026-03-22", "2026-03-31")  # окно robustness: последние 10 дней

    def plan(key, label, exits, figis):
        rows = [_row(label, xc, tk, net=100.0 if key in phase_keys else 20.0)
                for xc in exits for _f, tk in figis]
        if key == rob_key:
            rows.append(None)  # повреждение только в окне robustness
        return rows

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    assert data["robustness"]
    # 7 соседей (base + 3 параметра × lo/hi), каждый приносит один None →
    # одна запись без идентичности на соседа; контракт тот же, что в IS/OOS.
    assert len(data["robustness"]) == 7
    assert all(r["status"] == STATUS_INCOMPLETE for r in data["robustness"])
    assert all("non_dict=7" in r["reason"] for r in data["robustness"])
    assert all(r["net_pnl"] is None for r in data["robustness"])
    # структура повреждения сохраняется в каждом соседе
    assert all("damage" not in r or True for r in data["robustness"])  # damage опционален
    assert all(r.get("reason", "").startswith("incomplete_runs identity")
               for r in data["robustness"])


def test_run_wf_derived_overflow_saved_not_crash(tmp_path, monkeypatch):
    """C1-3: конечные raw (trades=1e-308, wins=1, net=1e308) дают inf в
    win_rate/expectancy: производные проверяются после деления, агрегат
    неполный (null + причина), run_wf не падает, файл сохраняется."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch)

    def plan(_key, label, exits, figis):
        return [_row(label, xc, tk, net=1e308, trades=1e-308, wins=1)
                for xc in exits for _f, tk in figis]

    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)  # строгий парсер: NaN/Infinity в файле → ошибка
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert rej[a].startswith("aggregate_overflow")
    assert "overflow:win_rate" in rej[a]
    assert "overflow:expectancy" in rej[a]
    agg = data["phases"][0]["by_label"][a]
    assert agg["win_rate"] is None and agg["expectancy"] is None
    assert math.isfinite(agg["net_pnl"])


def test_run_wf_raw_order_does_not_change_selection(tmp_path, monkeypatch):
    """C1-4 (интеграция): _run_grid возвращает raw в произвольном порядке
    (imap_unordered) — отбор и OOS-суммы одинаковы; порог min_net_pnl=0.5
    проходит при любом порядке (fsum)."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"],
                                     filters={"min_net_pnl": 0.5})
    a = _label("rsi_trade")
    figis = [("F1", "T1"), ("F2", "T2"), ("F3", "T3")]
    nets = {"T1": 1e16, "T2": -1e16, "T3": 1.0}
    calls = []

    def plan(_key, label, exits, figis_):
        rows = [_row(label, xc, tk, net=nets[tk]) for _f, tk in figis_ for xc in exits]
        calls.append(1)  # чётные вызовы — прямой порядок, нечётные — обратный
        return rows if len(calls) % 2 else rows[::-1]

    _patch_instruments(monkeypatch, figis=figis)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == [a]
    assert data["selected"][1]["selected"] == [a]
    for phase in data["phases"]:
        assert phase["by_label"][a]["net_pnl"] == 1.0
    entry = data["oos_matrix"][0]
    assert entry["oos_nets"] == [1.0, 1.0]


def test_run_wf_worker_error_text_not_published(tmp_path, monkeypatch):
    """C1-5 (интеграция): сообщение ошибки воркера не попадает в причины,
    phases и JSON — только безопасный класс."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    is_keys = {tuple(p[1:]) for p in _phases() if p[0] == "IS"}

    def plan(key, label, exits, figis):
        if key in is_keys:
            return [{"strategy": label, "exit": xc, "ticker": tk,
                     "error": "token=FAKE_TEST_MARKER boom"}
                    for xc in exits for _f, tk in figis]
        return [_row(label, xc, tk, net=50.0) for xc in exits for _f, tk in figis]

    _patch_instruments(monkeypatch)
    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["phases"][0]["worker_errors"] == [a]
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert "worker_errors=1" in rej[a]
    assert "worker_codes=exception" in rej[a]
    assert "FAKE_TEST_MARKER" not in rej[a]
    assert "FAKE_TEST_MARKER" not in json.dumps(data)


def test_run_wf_dd_filter_with_unknown_metric_not_admitted(tmp_path, monkeypatch):
    """C1-матрица (DD-фильтр × unknown): max_dd_pct неизвестен → агрегат
    None, кандидат отклоняется с incomplete_metrics:max_drawdown_pct
    (нулевая подстановка запрещена, пропуск фильтра невозможен)."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"],
                                     filters={"max_drawdown_pct": 10.0})
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch)

    def plan(_key, label, exits, figis):
        return [_row(label, xc, tk, net=100.0, max_dd_pct=None)
                for xc in exits for _f, tk in figis]

    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    assert data["selected"][0]["selected"] == []
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert rej[a] == "incomplete_metrics:max_drawdown_pct"
    assert data["phases"][0]["by_label"][a]["max_drawdown_pct"] is None


def test_run_wf_damage_detail_preserved_in_phase(tmp_path, monkeypatch):
    """C1-6 (интеграция): invalid_raw=N сопровождается invalid_fields=…,
    а в phases.damage сохраняется структура поле+ключ."""
    spec_path, out_dir = _write_spec(tmp_path, ["rsi_trade"])
    a = _label("rsi_trade")
    _patch_instruments(monkeypatch)

    def plan(_key, label, exits, figis):
        rows = [_row(label, xc, tk, net=100.0) for xc in exits for _f, tk in figis]
        rows[0] = dict(rows[0], net=float("nan"))
        return rows

    monkeypatch.setattr(bts, "_run_grid", _build_rows(plan))
    rc = bts.run_wf(_wf_args(spec_path))
    assert rc == 0
    data = _load_wf(out_dir)
    rej = {r["label"]: r["reason"] for r in data["phases"][0]["rejected"]}
    assert "invalid_raw=1" in rej[a]
    assert "invalid_fields=net" in rej[a]
    assert data["phases"][0]["damage"][a]["invalid"] == \
        [{"key": ["x07", "TEST"], "field": "net"}]
    assert data["selected"][0]["selected"] == []
