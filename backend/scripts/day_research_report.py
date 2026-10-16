#!/usr/bin/env python3
"""A11 offline-отчётчик сделок дневного эксперимента A7 (research-only, stdlib).

Задача A11 (ARCH 2026-10-09/10): по уже существующим артефактам A7 построить
достоверный воспроизводимый отчёт. Раны r2 закрывают пять блоков REWORK:
  1. входной контракт: обязательные конечные издержки, проверка config_hash A7,
     согласованность configs_detail/costs_semantics, конечные ATR>0 и денежные
     компоненты у FILLED; --phase ограничен development_unvalidated;
  2. dedup внутри рана и fail-closed отказ конфликтующих ранов (одна сущность
     ticker/config/day не более одного раза на сценарий);
  3. DQ/INCOMPLETE/rejected сохраняются и доходят до результата (run/scenario/report);
  4. разрезы: day_type (weekday/weekend/unknown) отдельной группой на всех уровнях;
     ticker=ALL без денежных средних/PF; R=net/(2*ATR) и ATR-единицы (=net/ATR);
     построчный trade_metrics.csv; нулевые ticker×config и месяцы периода;
  5. конечность всех производных до записи (иначе numerical rejection), immutable
     входы, документированный ненулевой exit при отказе.

Вход: явно переданные каталоги A7 (--run, повторяемый; manifest/summary/trades/
skipped/dq). Сценарий = (commission_bps, slippage_bps). Выход (--out; непустой
существующий запрещён): metrics.csv, trade_metrics.csv, report.md, rejects.csv,
runs_index.json, manifest.json.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path

ENGINE = "A7_research"
TF = "10min_signal/1min_execution"
SESSION = "main"
REGIME = "UNKNOWN"
PHASE_DEFAULT = "development_unvalidated"
FINAL_HOLDOUT = "NOT_ASSIGNED"
RESULT_VERDICT = "descriptive_only/profitability_unknown"
RUN_FILES = ("manifest.json", "summary.json", "trades.csv", "skipped.csv", "dq.csv")
STATUS_FILLED = "FILLED"
MONEY_NOT_PORTFOLIO_NOTE = "per-share/monetary metrics across different tickers are NOT a portfolio"
SKIPS_SIDE_NOTE = "skipped rows are not side-specific (side=ALL only)"
FLOAT_TOL = 1e-9
DAY_TYPES = ("weekday", "weekend", "unknown")

TRADE_NUMERIC = (
    "decision_close", "day_open", "half_ret", "gap", "cont", "threshold", "atr",
    "sl_level", "tp_level", "entry_base", "entry_fill", "slip_entry", "commission_entry",
    "exit_base", "exit_fill", "slip_exit", "commission_exit", "gross", "costs_total", "net",
)

METRIC_FIELDS = (
    "scenario", "phase", "final_holdout", "engine", "tf", "session", "regime",
    "scope", "ticker", "config", "side", "month_msk", "half", "day_type",
    "n_closed", "n_incomplete", "n_skipped", "n_rejected",
    "wins", "losses", "breakeven", "win_rate", "win_rate_denominator",
    "gross_sum", "commission_sum", "slippage_sum", "net_sum",
    "avg_win", "avg_loss", "profit_factor",
    "sum_R", "mean_R", "avg_win_R", "avg_loss_R", "atr_units_sum", "atr_units_mean",
    "closed_trade_drawdown", "drawdown_label",
    "money_not_portfolio", "note",
)
TRADE_METRIC_FIELDS = (
    "scenario", "origin_run_id", "origin_manifest_sha256", "ticker", "config", "side",
    "day", "month_msk", "day_type", "half", "status",
    "decision_label", "entry_ts", "exit_ts", "exit_reason", "incomplete_reason",
    "atr", "gross", "costs_total", "net", "R", "atr_units", "rejected",
)
REJECT_FIELDS = ("scenario", "run_id", "ticker", "config", "day", "entry_ts", "kind", "code", "detail")
DQ_SUMMARY_FIELDS = ("scope", "scenario", "run_id", "ticker", "overall_status",
                     "dq_errors", "dq_notices", "non_additive")


class ReportError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _num(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _config_hash(config_obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(config_obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _cost_key_of(manifest: dict, run_dir: Path) -> tuple:
    cfg = manifest.get("config")
    if not isinstance(cfg, dict):
        raise ReportError(f"{run_dir}: manifest.config отсутствует/не объект")
    for field, upper in (("commission_bps", None), ("slippage_bps", None)):
        if field not in cfg:
            raise ReportError(f"{run_dir}: manifest.config.{field} отсутствует (нельзя считать 0)")
    try:
        commission = float(cfg["commission_bps"])
        slippage = float(cfg["slippage_bps"])
    except (TypeError, ValueError):
        raise ReportError(f"{run_dir}: нечисловые commission_bps/slippage_bps") from None
    if not (math.isfinite(commission) and math.isfinite(slippage)):
        raise ReportError(f"{run_dir}: неконечные commission_bps/slippage_bps")
    if commission < 0:
        raise ReportError(f"{run_dir}: commission_bps<0")
    if not (0 <= slippage < 10000):
        raise ReportError(f"{run_dir}: slippage_bps вне [0,10000)")
    declared = manifest.get("config_hash")
    if declared is not None:
        recomputed = _config_hash(cfg)
        if recomputed != declared:
            raise ReportError(f"{run_dir}: config_hash не совпадает с пересчётом по manifest.config")
    return (commission, slippage)


def _scenario_name(key: tuple) -> str:
    return f"costs_{key[0]:g}_{key[1]:g}bps"


def _day_type(day: str) -> str:
    try:
        d = date.fromisoformat(day)
    except (ValueError, TypeError):
        return "unknown"
    return "weekday" if d.weekday() < 5 else "weekend"


def _month_msk(day: str) -> str:
    return day[:7] if len(day) >= 7 else "unknown"


def load_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    missing = [name for name in RUN_FILES if not (run_dir / name).exists()]
    if missing:
        raise ReportError(f"{run_dir}: отсутствуют файлы {missing}")
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{run_dir}: невалидный JSON manifest/summary: {exc}") from None
    cost_key = _cost_key_of(manifest, run_dir)
    trades = _read_csv(run_dir / "trades.csv")
    skips = _read_csv(run_dir / "skipped.csv")
    dq = _read_csv(run_dir / "dq.csv")

    out_hashes = manifest.get("output_sha256") or {}
    for name in ("trades.csv", "skipped.csv", "dq.csv", "summary.json"):
        if name in out_hashes and _sha256_file(run_dir / name) != out_hashes[name]:
            raise ReportError(f"{run_dir}: output_sha256 не совпадает для {name}")
    if "counts" in summary:
        cnt = summary["counts"]
        for actual, declared, field in ((len(trades), cnt.get("trades_total"), "trades_total"),
                                        (len(skips), cnt.get("skipped"), "skipped"),
                                        (len(dq), cnt.get("dq_issues"), "dq_issues")):
            if declared is not None and int(declared) != actual:
                raise ReportError(f"{run_dir}: counts.{field}={declared} != факта {actual}")

    parsed = []
    for row in trades:
        rec = {k: (str(row.get(k)).strip() if row.get(k) is not None else "") for k in row}
        for field in TRADE_NUMERIC:
            rec[field] = _num(row.get(field))
        parsed.append(rec)

    dq_err = sum(1 for r in dq if (r.get("kind") or "").strip() == "error")
    dq_note = sum(1 for r in dq if (r.get("kind") or "").strip() == "notice")
    return {
        "run_dir": str(run_dir.resolve()),
        "run_id": str(manifest.get("run_id") or run_dir.name),
        "manifest_sha256": _sha256_file(run_dir / "manifest.json"),
        "cost_key": cost_key,
        "manifest": manifest,
        "summary": summary,
        "trades": parsed,
        "skips": skips,
        "dq": dq,
        "dq_errors": dq_err,
        "dq_notices": dq_note,
        "overall_status": str(summary.get("overall_status") or ""),
        "file_sha256": {name: _sha256_file(run_dir / name) for name in RUN_FILES},
    }


def _identity(trade: dict) -> tuple:
    return (trade.get("ticker", ""), trade.get("config", ""),
            trade.get("day", ""), trade.get("entry_ts", ""))


def _entity_key(rec: dict) -> tuple:
    return (rec.get("ticker", ""), rec.get("config", ""), rec.get("day", ""))


def _closed_trade_drawdown(closed: list[dict]):
    seq = [t for t in closed if t.get("net") is not None]
    if not seq:
        return None
    seq = sorted(seq, key=lambda t: (t.get("exit_ts") or t.get("day") or "",
                                     t.get("ticker", ""), t.get("config", "")))
    cum = peak = worst = 0.0
    for t in seq:
        cum += t["net"]
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def _cell_metrics(trades, skips, n_rejected, count_skips, money_allowed):
    closed = [t for t in trades if t.get("status") == STATUS_FILLED]
    incomplete = [t for t in trades if t.get("status") != STATUS_FILLED]
    nets = [t["net"] for t in closed if t.get("net") is not None]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    breakeven = [n for n in nets if n == 0]
    n_closed = len(closed)

    if n_closed == 0:
        pf = "no_trades"
    elif not losses:
        pf = "no_losses"
    elif sum(wins) == 0.0:
        pf = 0.0
    else:
        pf = sum(wins) / abs(sum(losses))

    r_vals = [t["net"] / (2.0 * t["atr"]) for t in closed
              if t.get("net") is not None and t.get("atr")]
    atr_vals = [t["net"] / t["atr"] for t in closed
                if t.get("net") is not None and t.get("atr")]
    wins_r = [t["net"] / (2.0 * t["atr"]) for t in closed
              if t.get("net") is not None and t["net"] > 0 and t.get("atr")]
    loss_r = [t["net"] / (2.0 * t["atr"]) for t in closed
              if t.get("net") is not None and t["net"] < 0 and t.get("atr")]
    dd = _closed_trade_drawdown(closed) if n_closed else None

    def money(v):
        return v if money_allowed else None

    return {
        "n_closed": n_closed,
        "n_incomplete": len(incomplete),
        "n_skipped": (len(skips) if count_skips else None),
        "n_rejected": n_rejected,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": (len(wins) / n_closed) if n_closed else None,
        "win_rate_denominator": n_closed,
        "gross_sum": money(sum(t["gross"] for t in closed if t.get("gross") is not None)
                           if n_closed else None),
        "commission_sum": money(sum((t.get("commission_entry") or 0.0)
                                    + (t.get("commission_exit") or 0.0) for t in closed)
                                if n_closed else None),
        "slippage_sum": money(sum((t["costs_total"] - (t.get("commission_entry") or 0.0)
                                   - (t.get("commission_exit") or 0.0))
                                  for t in closed if t.get("costs_total") is not None)
                              if n_closed else None),
        "net_sum": money(sum(nets) if n_closed else None),
        "avg_win": money((sum(wins) / len(wins)) if wins else None),
        "avg_loss": money((sum(losses) / len(losses)) if losses else None),
        "profit_factor": money(pf),
        "sum_R": (sum(r_vals) if r_vals else None),
        "mean_R": (sum(r_vals) / len(r_vals)) if r_vals else None,
        "avg_win_R": (sum(wins_r) / len(wins_r)) if wins_r else None,
        "avg_loss_R": (sum(loss_r) / len(loss_r)) if loss_r else None,
        "atr_units_sum": (sum(atr_vals) if atr_vals else None),
        "atr_units_mean": (sum(atr_vals) / len(atr_vals)) if atr_vals else None,
        "closed_trade_drawdown": money(dd),
        "drawdown_label": ("realized_pnl_sequence_not_portfolio"
                           if (dd is not None and money_allowed) else ""),
    }


def _row(scenario, scope, ticker, config, side, month_msk, half, day_type, m, money_allowed,
         count_skips):
    notes = []
    if not money_allowed and (ticker == "ALL"):
        notes.append(MONEY_NOT_PORTFOLIO_NOTE)
    if not count_skips:
        notes.append(SKIPS_SIDE_NOTE)
    return {
        "scenario": scenario, "phase": PHASE_DEFAULT, "final_holdout": FINAL_HOLDOUT,
        "engine": ENGINE, "tf": TF, "session": SESSION, "regime": REGIME,
        "scope": scope, "ticker": ticker, "config": config, "side": side,
        "month_msk": month_msk, "half": half, "day_type": day_type,
        "n_closed": m["n_closed"], "n_incomplete": m["n_incomplete"],
        "n_skipped": m["n_skipped"], "n_rejected": m["n_rejected"],
        "wins": m["wins"], "losses": m["losses"], "breakeven": m["breakeven"],
        "win_rate": m["win_rate"], "win_rate_denominator": m["win_rate_denominator"],
        "gross_sum": m["gross_sum"], "commission_sum": m["commission_sum"],
        "slippage_sum": m["slippage_sum"], "net_sum": m["net_sum"],
        "avg_win": m["avg_win"], "avg_loss": m["avg_loss"], "profit_factor": m["profit_factor"],
        "sum_R": m["sum_R"], "mean_R": m["mean_R"],
        "avg_win_R": m["avg_win_R"], "avg_loss_R": m["avg_loss_R"],
        "atr_units_sum": m["atr_units_sum"], "atr_units_mean": m["atr_units_mean"],
        "closed_trade_drawdown": m["closed_trade_drawdown"],
        "drawdown_label": m["drawdown_label"],
        "money_not_portfolio": (not money_allowed),
        "note": "; ".join(notes),
    }


def _months_between(start: str, end: str) -> list[str]:
    try:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
    except (ValueError, TypeError):
        return []
    out = []
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _half_split(runs: list[dict]):
    starts, ends = [], []
    for r in runs:
        cfg = r["manifest"].get("config") or {}
        try:
            starts.append(date.fromisoformat(cfg["eval_start"]))
            ends.append(date.fromisoformat(cfg["eval_end"]))
        except (KeyError, ValueError, TypeError):
            continue
    if not starts:
        return None, None, None, None
    lo, hi = min(starts), max(ends)
    return lo, lo + (hi - lo) // 2, lo.isoformat(), hi.isoformat()


def _half_of(day: str, lo, mid) -> str:
    try:
        d = date.fromisoformat(day)
    except (ValueError, TypeError):
        return "unknown"
    if lo is None:
        return "unknown"
    return "H1" if d <= mid else "H2"


def _reject(scenario, rec, kind, code, detail, origin=("", "")):
    o = rec.get("_origin") or origin
    return {"scenario": scenario, "run_id": o[0], "ticker": rec.get("ticker", ""),
            "config": rec.get("config", ""), "day": rec.get("day", ""),
            "entry_ts": rec.get("entry_ts", ""), "kind": kind, "code": code, "detail": detail}


def build_scenario(name, runs, rejects):
    lo, mid, win_start, win_end = _half_split(runs)
    declared_configs = set()
    for r in runs:
        declared_configs.update((r["manifest"].get("config") or {}).get("configs") or [])

    trades, skips = [], []
    for r in runs:
        origin = (r["run_id"], r["manifest_sha256"])
        for t in r["trades"]:
            tt = dict(t)
            tt["_origin"] = origin
            trades.append(tt)
        for s in r["skips"]:
            ss = dict(s)
            ss["_origin"] = origin
            skips.append(ss)

    groups = {}
    for t in trades:
        groups.setdefault((t["_origin"], _identity(t)), []).append(t)
    deduped = []
    for grp in groups.values():
        if len(grp) == 1:
            deduped.append(grp[0])
            continue
        stripped = [{k: v for k, v in g.items() if k != "_origin"} for g in grp]
        if all(s == stripped[0] for s in stripped):
            deduped.append(grp[0])
            for g in grp[1:]:
                rejects.append(_reject(name, g, "duplicate", "duplicate_trade_in_run",
                                       "идентичная копия в одном ране"))
        else:
            for g in grp:
                rejects.append(_reject(name, g, "conflict", "conflicting_duplicate_in_run",
                                       "различные поля при одной идентичности в одном ране"))
    seen_skip = set()
    deduped_skips = []
    for s in skips:
        k = (s["_origin"], s.get("ticker", ""), s.get("config", ""), s.get("day", ""),
             s.get("reason", ""))
        if k in seen_skip:
            continue
        seen_skip.add(k)
        deduped_skips.append(s)

    key_runs = {}
    for idx, r in enumerate(runs):
        keys = {_entity_key(t) for t in r["trades"]} | {_entity_key(s) for s in r["skips"]}
        for k in keys:
            key_runs.setdefault(k, set()).add(idx)
    conflict_runs = set()
    for k, idxs in key_runs.items():
        if len(idxs) > 1:
            conflict_runs |= idxs
    run_index = {r["run_id"] + "|" + r["manifest_sha256"]: i for i, r in enumerate(runs)}

    final_trades, final_skips = [], []
    for t in deduped:
        idx = run_index[t["_origin"][0] + "|" + t["_origin"][1]]
        if idx in conflict_runs:
            continue
        if t.get("status") == STATUS_FILLED:
            need = [t.get("gross"), t.get("costs_total"), t.get("net"), t.get("atr"),
                    t.get("commission_entry"), t.get("commission_exit"),
                    t.get("entry_fill"), t.get("exit_fill")]
            if any(v is None for v in need):
                rejects.append(_reject(name, t, "structural", "filled_missing_component",
                                       "FILLED без конечного ATR/денежного компонента"))
                continue
            if not t["atr"] > 0:
                rejects.append(_reject(name, t, "structural", "filled_nonpositive_atr",
                                       f"ATR={t['atr']}"))
                continue
            if abs(t["net"] - (t["gross"] - t["costs_total"])) > FLOAT_TOL * (
                    1.0 + abs(t["net"]) + abs(t["gross"])):
                rejects.append(_reject(name, t, "structural", "net_identity_mismatch",
                                       f"net={t['net']} != gross-costs={t['gross'] - t['costs_total']}"))
                continue
            comm = (t["commission_entry"] or 0.0) + (t["commission_exit"] or 0.0)
            sign = 1 if t.get("side") == "long" else -1
            slip_cost = t["gross"] - sign * (t["exit_fill"] - t["entry_fill"])
            if abs(t["costs_total"] - (comm + slip_cost)) > FLOAT_TOL * (
                    1.0 + abs(t["costs_total"]) + abs(comm) + abs(slip_cost)):
                rejects.append(_reject(name, t, "structural", "costs_component_mismatch",
                                       f"costs_total={t['costs_total']} != commission+slip="
                                       f"{comm + slip_cost}"))
                continue
        final_trades.append(t)
    for s in deduped_skips:
        idx = run_index[s["_origin"][0] + "|" + s["_origin"][1]]
        if idx in conflict_runs:
            continue
        final_skips.append(s)
    for idx in sorted(conflict_runs):
        r = runs[idx]
        shared = sorted(f"{k[0]}/{k[1]}/{k[2]}"
                        for k, v in key_runs.items() if idx in v and len(v) > 1)
        rejects.append({"scenario": name, "run_id": r["run_id"], "ticker": "", "config": "",
                        "day": "", "entry_ts": "", "kind": "conflict", "code": "conflicting_runs",
                        "detail": "пересечение ticker/config/day с другим раном: " + ",".join(shared[:5])})

    declared_tickers = sorted({t["ticker"] for t in final_trades}
                              | {s["ticker"] for s in final_skips})
    months = _months_between(win_start, win_end)

    def match_trade(t, ticker, config, side, month, half, day_type):
        if ticker != "ALL" and t["ticker"] != ticker:
            return False
        if config != "ALL" and t["config"] != config:
            return False
        if side != "ALL" and t["side"] != side:
            return False
        if month != "ALL" and _month_msk(t["day"]) != month:
            return False
        if half != "ALL" and _half_of(t["day"], lo, mid) != half:
            return False
        return _day_type(t["day"]) == day_type

    def match_skip(s, ticker, config, month, half, day_type):
        if ticker != "ALL" and s.get("ticker") != ticker:
            return False
        if config != "ALL" and s.get("config") != config:
            return False
        if month != "ALL" and _month_msk(s.get("day", "")) != month:
            return False
        if half != "ALL" and _half_of(s.get("day", ""), lo, mid) != half:
            return False
        return _day_type(s.get("day", "")) == day_type

    def match_rej(rj, ticker, config, month, half, day_type):
        if ticker != "ALL" and rj["ticker"] not in ("", ticker):
            return False
        if config != "ALL" and rj["config"] not in ("", config):
            return False
        if month != "ALL" and rj["day"] and _month_msk(rj["day"]) != month:
            return False
        if half != "ALL" and rj["day"] and _half_of(rj["day"], lo, mid) != half:
            return False
        if rj["day"] and _day_type(rj["day"]) != day_type:
            return False
        return True

    rows = []
    built = set()

    def emit(scope, ticker, config, side, month, half, day_type):
        key = (scope, ticker, config, side, month, half, day_type)
        if key in built:
            return
        built.add(key)
        ct = [t for t in final_trades
              if match_trade(t, ticker, config, side, month, half, day_type)]
        count_skips = (side == "ALL")
        cs = ([s for s in final_skips
               if match_skip(s, ticker, config, month, half, day_type)] if count_skips else [])
        rj = [r for r in rejects
              if r["scenario"] == name and r["ticker"]
              and match_rej(r, ticker, config, month, half, day_type)]
        n_rej = len(rj) if count_skips else None
        money_allowed = ticker != "ALL"
        m = _cell_metrics(ct, cs, n_rej, count_skips, money_allowed)
        rows.append(_row(name, scope, ticker, config, side, month, half, day_type, m,
                         money_allowed, count_skips))

    for dt in DAY_TYPES:
        emit("scenario", "ALL", "ALL", "ALL", "ALL", "ALL", dt)
        for mo in months:
            emit("scenario_month", "ALL", "ALL", "ALL", mo, "ALL", dt)
        for hf in ("H1", "H2"):
            emit("scenario_half", "ALL", "ALL", "ALL", "ALL", hf, dt)
        for tk in declared_tickers:
            emit("ticker", tk, "ALL", "ALL", "ALL", "ALL", dt)
            emit("ticker_half", tk, "ALL", "ALL", "ALL", "H1", dt)
            emit("ticker_half", tk, "ALL", "ALL", "ALL", "H2", dt)
        for cfg in sorted(declared_configs):
            emit("config", "ALL", cfg, "ALL", "ALL", "ALL", dt)
            for mo in months:
                emit("config_month", "ALL", cfg, "ALL", mo, "ALL", dt)
            emit("config_half", "ALL", cfg, "ALL", "ALL", "H1", dt)
            emit("config_half", "ALL", cfg, "ALL", "ALL", "H2", dt)

    for dt in DAY_TYPES:
        for tk in declared_tickers:
            for cfg in sorted(declared_configs):
                emit("ticker_config", tk, cfg, "ALL", "ALL", "ALL", dt)
                for hf in ("H1", "H2"):
                    emit("ticker_config_half", tk, cfg, "ALL", "ALL", hf, dt)
        for tk in declared_tickers:
            for cfg in sorted(declared_configs):
                for sd in ("long", "short"):
                    emit("ticker_config_side", tk, cfg, sd, "ALL", "ALL", dt)
                    for hf in ("H1", "H2"):
                        emit("ticker_config_side_half", tk, cfg, sd, "ALL", hf, dt)

    for dt in DAY_TYPES:
        for tk in declared_tickers:
            for cfg in sorted(declared_configs):
                for mo in months:
                    emit("ticker_config_month", tk, cfg, "ALL", mo, "ALL", dt)
        for tk in declared_tickers:
            for cfg in sorted(declared_configs):
                for mo in months:
                    for sd in ("long", "short"):
                        emit("ticker_config_side_month", tk, cfg, sd, mo, "ALL", dt)

    rows = [r for r in rows if r is not None]
    rows.sort(key=lambda r: (r["scenario"], r["scope"], r["ticker"], r["config"], r["side"],
                             r["month_msk"], r["half"], r["day_type"]))
    info = {"half_split": mid.isoformat() if mid else None, "window": [win_start, win_end],
            "months": months, "declared_configs": sorted(declared_configs),
            "declared_tickers": declared_tickers}
    return rows, info, final_trades, final_skips


def _trade_metric_rows(name, trades, lo, mid):
    rows = []
    for t in trades:
        filled = t.get("status") == STATUS_FILLED
        atr = t.get("atr")
        net = t.get("net")
        rows.append({
            "scenario": name, "origin_run_id": t["_origin"][0],
            "origin_manifest_sha256": t["_origin"][1], "ticker": t["ticker"],
            "config": t["config"], "side": t["side"], "day": t["day"],
            "month_msk": _month_msk(t["day"]), "day_type": _day_type(t["day"]),
            "half": _half_of(t["day"], lo, mid), "status": t.get("status", ""),
            "decision_label": t.get("decision_label", ""), "entry_ts": t.get("entry_ts", ""),
            "exit_ts": t.get("exit_ts", ""), "exit_reason": t.get("exit_reason", ""),
            "incomplete_reason": t.get("incomplete_reason", ""),
            "atr": atr if filled else None, "gross": t.get("gross") if filled else None,
            "costs_total": t.get("costs_total") if filled else None,
            "net": net if filled else None,
            "R": (net / (2.0 * atr)) if (filled and net is not None and atr) else None,
            "atr_units": (net / atr) if (filled and net is not None and atr) else None,
            "rejected": False,
        })
    return rows


def _finite_check(obj, path):
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ReportError(f"nonfinite_derived:{path}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _finite_check(v, f"{path}.{k}")
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _finite_check(v, f"{path}[{i}]")


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _write_csv(path: Path, fields, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k, "")) for k in fields})


def _report_md(scenarios):
    lines = ["# A11 offline-отчёт по сделкам дневного эксперимента A7", "",
             f"- engine={ENGINE}; tf={TF}; session={SESSION}; regime={REGIME}",
             f"- phase={PHASE_DEFAULT}; final_holdout={FINAL_HOLDOUT}; verdict={RESULT_VERDICT}",
             f"- {MONEY_NOT_PORTFOLIO_NOTE}",
             "- R = net/(2*ATR); ATR-единицы = net/ATR; day_type отдельной группой",
             ""]
    for sc in scenarios:
        lines.append(f"## Сценарий `{sc['name']}` (commission={sc['costs'][0]:g} bps, "
                     f"slippage={sc['costs'][1]:g} bps)")
        lines.append("")
        lines.append(f"- runs={len(sc['runs'])}; window={sc['window']}; half_split={sc['half_split']}")
        lines.append(f"- gross_only={sc['gross_only']}; scenario_rejects={sc['scenario_rejects']}; "
                     f"conflicts={sc['conflicts']}")
        lines.append("- totals по day_type (счётчики; денежные суммы на ticker=ALL не портфель):")
        for dt in DAY_TYPES:
            s = sc["totals_by_day_type"][dt]
            lines.append(f"  - day_type={dt}: n_closed={s['n_closed']}; "
                         f"n_incomplete={s['n_incomplete']}; n_skipped={s['n_skipped']}; "
                         f"n_rejected={s['n_rejected']}")
        lines.append("")
        lines.append("| ticker | config | side | day_type | n_closed | n_incomp | n_rejected | "
                     "net_sum_1share | avg_win | PF | sum_R |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sc["table"]:
            lines.append(f"| {r['ticker']} | {r['config']} | {r['side']} | {r['day_type']} | "
                         f"{_fmt(r['n_closed'])} | {_fmt(r['n_incomplete'])} | {_fmt(r['n_rejected'])} | "
                         f"{_fmt(r['net_sum'])} | {_fmt(r['avg_win'])} | "
                         f"{_fmt(r['profit_factor'])} | {_fmt(r['sum_R'])} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A11 offline-отчёт по сделкам A7 (research-only)")
    ap.add_argument("--run", action="append", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--phase", default=PHASE_DEFAULT)
    args = ap.parse_args(argv)

    if args.phase != PHASE_DEFAULT:
        ap.error(f"--phase ограничен '{PHASE_DEFAULT}' (OOS/holdout не назначались)")
    out = args.out
    if out.exists() and any(out.iterdir()):
        ap.error(f"--out {out} не пуст (immutable); документированный отказ, exit 2")
    out.mkdir(parents=True, exist_ok=True)

    rejects: list[dict] = []
    runs, seen = [], {}
    for rd in args.run:
        r = load_run(rd)
        key = (r["run_id"], r["manifest_sha256"])
        if key in seen:
            rejects.append({"scenario": "", "run_id": r["run_id"], "ticker": "", "config": "",
                            "day": "", "entry_ts": "", "kind": "duplicate",
                            "code": "duplicate_run_input", "detail": f"повтор {rd}"})
            continue
        seen[key] = str(rd)
        runs.append(r)

    by_scenario = {}
    for r in runs:
        by_scenario.setdefault(r["cost_key"], []).append(r)

    for key, rs in by_scenario.items():
        codes = {json.dumps(r["manifest"].get("code_sha256") or {}, sort_keys=True) for r in rs}
        if len(codes) > 1:
            raise ReportError(f"{_scenario_name(key)}: разные code_sha256 у ранов")
        semantics = {(r["manifest"].get("costs_semantics") or "") for r in rs}
        if len(semantics) > 1:
            raise ReportError(f"{_scenario_name(key)}: разные costs_semantics у ранов")
        detail = {}
        for r in rs:
            for entry in ((r["manifest"].get("config") or {}).get("configs_detail") or []):
                nm = entry.get("config")
                if nm in detail and detail[nm] != entry:
                    raise ReportError(f"{_scenario_name(key)}: конфликт configs_detail для {nm}")
                detail[nm] = entry

    scenario_infos = []
    all_rows, all_trade_rows, all_rejects = [], [], list(rejects)
    dq_summary = []
    for key in sorted(by_scenario):
        name = _scenario_name(key)
        rows, info, ftrades, fskips = build_scenario(name, by_scenario[key], all_rejects)
        all_rows.extend(rows)
        all_trade_rows.extend(_trade_metric_rows(name, ftrades,
                                                 *(_half_split(by_scenario[key])[:2])))
        run_rej = [x for x in all_rejects if x["scenario"] == name]
        totals_by_dt = {}
        for dt in DAY_TYPES:
            dt_tr = [t for t in ftrades if _day_type(t["day"]) == dt]
            dt_sk = [s for s in fskips if _day_type(s["day"]) == dt]
            dt_rej = [x for x in run_rej if x["ticker"] and _day_type(x["day"]) == dt]
            totals_by_dt[dt] = _cell_metrics(dt_tr, dt_sk, len(dt_rej), True, False)
        gross_only = bool((by_scenario[key][0]["summary"] or {}).get("gross_only"))
        scenario_infos.append({
            "name": name, "costs": key, "runs": [r["run_dir"] for r in by_scenario[key]],
            "window": info["window"], "half_split": info["half_split"], "gross_only": gross_only,
            "conflicts": len([x for x in run_rej if x["kind"] == "conflict"]),
            "totals_by_day_type": totals_by_dt, "scenario_rejects": len(run_rej),
            "table": [r for r in rows if r["scope"] == "ticker_config_side"]})
        for r in by_scenario[key]:
            dq_summary.append({"scope": "run", "scenario": name, "run_id": r["run_id"],
                               "ticker": "ALL", "overall_status": r["overall_status"],
                               "dq_errors": r["dq_errors"], "dq_notices": r["dq_notices"],
                               "non_additive": "true"})
            per_ticker = {}
            for d in r["dq"]:
                tk = (d.get("ticker") or "UNKNOWN")
                kind = (d.get("kind") or "")
                per_ticker[(tk, kind)] = per_ticker.get((tk, kind), 0) + 1
            for (tk, kind), n in sorted(per_ticker.items()):
                dq_summary.append({"scope": "run_ticker", "scenario": name, "run_id": r["run_id"],
                                   "ticker": tk, "overall_status": r["overall_status"],
                                   "dq_errors": n if kind == "error" else 0,
                                   "dq_notices": n if kind == "notice" else 0,
                                   "non_additive": "true"})

    for row in all_rows:
        _finite_check(row, f"metric[{row['scope']}/{row['ticker']}/{row['config']}/{row['day_type']}]")
    for row in all_trade_rows:
        _finite_check(row, f"trade_metric[{row['ticker']}/{row['config']}/{row['entry_ts']}]")

    _write_csv(out / "metrics.csv", METRIC_FIELDS, all_rows)
    _write_csv(out / "trade_metrics.csv", TRADE_METRIC_FIELDS, all_trade_rows)
    _write_csv(out / "rejects.csv", REJECT_FIELDS, all_rejects)
    _write_csv(out / "dq_summary.csv", DQ_SUMMARY_FIELDS, dq_summary)
    (out / "report.md").write_text(_report_md(scenario_infos), encoding="utf-8")
    runs_index = [{"run_dir": r["run_dir"], "run_id": r["run_id"],
                   "manifest_sha256": r["manifest_sha256"], "cost_key": list(r["cost_key"]),
                   "overall_status": r["overall_status"], "dq_errors": r["dq_errors"],
                   "dq_notices": r["dq_notices"], "file_sha256": r["file_sha256"]}
                  for r in runs]
    (out / "runs_index.json").write_text(
        json.dumps(runs_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "run_id": out.name, "tool": "day_research_report.py", "phase": args.phase,
        "final_holdout": FINAL_HOLDOUT, "result_verdict": RESULT_VERDICT,
        "engine": ENGINE, "tf": TF, "session": SESSION, "regime": REGIME,
        "units": {"R": "net/(2*ATR)", "atr_units": "net/ATR"},
        "inputs": runs_index, "input_run_dirs": [r["run_dir"] for r in runs],
        "code_sha256": {"day_research_report.py": _sha256_file(Path(__file__).resolve())},
        "scenarios": [{"name": s["name"], "costs_bps": list(s["costs"]),
                       "gross_only": s["gross_only"], "window": s["window"],
                       "half_split": s["half_split"], "runs": s["runs"]} for s in scenario_infos],
        "rejects": all_rejects,
        "output_sha256": {n: _sha256_file(out / n) for n in
                          ("metrics.csv", "trade_metrics.csv", "report.md", "rejects.csv",
                           "dq_summary.csv", "runs_index.json")},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(f"A11 report: {out} (scenarios={len(scenario_infos)}, metrics={len(all_rows)}, "
          f"trades={len(all_trade_rows)}, rejects={len(all_rejects)})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"A11 rejected: {exc}", file=sys.stderr)
        raise SystemExit(2)
