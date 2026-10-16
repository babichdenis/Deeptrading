#!/usr/bin/env python3
"""BATCH-15 runner (B2). Принимает --group a|b|c; читает ТОЛЬКО указанную группу.

Загружает каждый тикер один раз (из --input-root/<ticker>.csv через a7.load_minute_csv),
переиспользует TickerData для всех кандидатов/сценариев costs группы. Каждая ячейка
candidate×ticker×cost пишется в свой каталог; общий итог — в _aggregate/.

Исполнение — существующий A24 execute_candidate_signal (общий research_execution);
нового движка/settlement нет. Встроенные проверки: spec/input/code/interpreter
fingerprints, causality (decision==entry, minute-aligned), net=gross-costs,
уникальный event_id, отсутствие числового net у INCOMPLETE, статус каждой ячейки.
Ошибочная ячейка помечается ERROR, остальные продолжаются; тикер/кандидат не
сбрасываются молча.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import day_strategies_core as a7
import day_candidate_execution as a24

TOOL = "run_batch15"
NET_TOL = 1e-6

TICKERS_DEFAULT = ("GAZP", "LKOH", "SFIN", "VKCO")
WARMUP_START_DEFAULT = date(2026, 6, 8)
EVAL_START_DEFAULT = date(2026, 6, 11)
EVAL_END_DEFAULT = date(2026, 10, 8)
COST_SCENARIOS = (
    (5.0, 2.0, "c5_s2"),
    (10.0, 5.0, "c10_s5"),
)

TRADE_FIELDS = (
    "ticker", "candidate_id", "day", "side", "spec_hash", "signal_hash",
    "decision_label", "decision_close_time", "observed_until", "intended_entry_time",
    "atr", "sl_level", "tp_level", "entry_ts", "entry_base", "entry_fill",
    "slip_entry", "commission_entry", "exit_ts", "exit_base", "exit_fill",
    "slip_exit", "commission_exit", "exit_reason", "gross", "costs_total", "net",
    "status", "incomplete_reason", "detail", "event_id",
)
SKIP_FIELDS = ("ticker", "candidate_id", "day", "event_type", "reason", "detail")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _weekdays(start: date, end: date) -> list:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += __import__("datetime").timedelta(days=1)
    return out


def _write_csv(path: Path, fields: tuple, rows: list) -> None:
    import csv as _csv
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(fields)
        for r in rows:
            w.writerow([r.get(f, "") if isinstance(r, dict) else "" for f in fields])


def _by_month(trades: list, skips: list) -> dict:
    out: dict = {}
    for t in trades:
        m = t["day"][:7]
        cell = out.setdefault(m, {"filled": 0, "incomplete": 0, "net_sum": 0.0,
                                  "gross_sum": 0.0, "costs_sum": 0.0})
        if t["status"] == "FILLED":
            cell["filled"] += 1
            try:
                cell["net_sum"] += float(t["net"])
                cell["gross_sum"] += float(t["gross"])
                cell["costs_sum"] += float(t["costs_total"])
            except (TypeError, ValueError):
                pass
        elif t["status"] == "INCOMPLETE":
            cell["incomplete"] += 1
    for s in skips:
        m = s.get("day", "")[:7]
        out.setdefault(m, {"filled": 0, "incomplete": 0, "net_sum": 0.0,
                           "gross_sum": 0.0, "costs_sum": 0.0})
    return out


def _trade_row(rec: dict, event_id: str) -> dict:
    row = dict(rec)
    row["event_id"] = event_id
    return row


def _run_cell(mod, tdata, cid, ticker, costs, cost_id, days, group, out_dir: Path,
              code_sha: dict, preflight_common: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_hash = mod.SPEC_HASHES[cid]
    trades: list = []
    skips: list = []
    signals: list = []
    rejects: list = []
    issues = []
    event_ids: set = set()
    net_identity_ok = True
    incomplete_has_net = False
    cell_error = None

    for day in days:
        ev = mod.evaluate(cid, tdata, day)
        event_id = f"{cid}|{ticker}|{day.isoformat()}"
        if event_id in event_ids:
            cell_error = f"duplicate event_id {event_id}"
        event_ids.add(event_id)

        if ev["type"] == "signal":
            signals.append(ev)
            rec = a24.execute_candidate_signal(
                ev, tdata, costs, expected_spec_hash=spec_hash)
            status = rec.get("status")
            if status == "REJECT":
                rejects.append({"day": day.isoformat(), "reason": rec.get("incomplete_reason"),
                               "detail": rec.get("detail")})
                (out_dir / "rejected_signal.json").write_text(
                    json.dumps({"signal": ev, "result": rec}, ensure_ascii=False,
                               indent=2, allow_nan=False), encoding="utf-8")
                continue
            if status in ("FILLED", "INCOMPLETE"):
                if status == "INCOMPLETE" and rec.get("net", "") not in ("", None):
                    incomplete_has_net = True
                if status == "FILLED":
                    try:
                        g = float(rec["gross"]); c = float(rec["costs_total"])
                        n = float(rec["net"])
                        if abs(n - (g - c)) > NET_TOL:
                            net_identity_ok = False
                    except (TypeError, ValueError):
                        net_identity_ok = False
                trades.append(_trade_row(rec, event_id))
            else:
                skips.append({"ticker": ticker, "candidate_id": cid,
                              "day": day.isoformat(), "event_type": "skip",
                              "reason": rec.get("incomplete_reason", "skip"),
                              "detail": rec.get("detail", "")})
        else:
            skips.append({"ticker": ticker, "candidate_id": cid,
                          "day": ev["day"], "event_type": ev["type"],
                          "reason": ev.get("reason", ev["type"]),
                          "detail": ev.get("detail", "")})

    if incomplete_has_net:
        cell_error = cell_error or "INCOMPLETE trade carries numeric net"
    if not net_identity_ok:
        cell_error = cell_error or "net != gross - costs_total for a FILLED trade"
    if rejects:
        cell_error = cell_error or f"{len(rejects)} rejected signal(s)"

    # --- per-cell fingerprints / manifest
    preflight = dict(preflight_common)
    preflight.update({
        "candidate_id": cid,
        "spec_hash": spec_hash,
        "spec": mod.SPECS[cid],
        "ticker": ticker,
        "cost_scenario": cost_id,
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
    })
    (out_dir / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")

    _write_csv(out_dir / "trades.csv", TRADE_FIELDS, trades)
    _write_csv(out_dir / "skipped.csv", SKIP_FIELDS, skips)
    (out_dir / "signals.json").write_text(
        json.dumps(signals, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")

    filled = [t for t in trades if t["status"] == "FILLED"]
    inc = [t for t in trades if t["status"] == "INCOMPLETE"]
    nets = [float(t["net"]) for t in filled]
    ratios = [float(t["net"]) / float(t["entry_base"])
              for t in filled if t.get("entry_base") not in ("", None)]
    status_parts = []
    if inc:
        status_parts.append("INCOMPLETE_TRADES")
    if cell_error:
        status_parts.append("CELL_ERROR")
    overall = "+".join(status_parts) if status_parts else "OK"

    summary = {
        "candidate_id": cid,
        "ticker": ticker,
        "cost_scenario": cost_id,
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "spec_hash": spec_hash,
        "days_evaluated": len(days),
        "signals": len(signals),
        "FILLED": len(filled),
        "INCOMPLETE": len(inc),
        "SKIP": sum(1 for s in skips if s.get("event_type") == "skip"),
        "insufficient_context": sum(1 for s in skips
                                   if s.get("event_type") == "insufficient_context"),
        "REJECT": len(rejects),
        "gross_only": costs.gross_only,
        "overall_status": overall,
        "cell_error": cell_error,
        "net_note": ("net/entry_base — для ОДНОГО инструмента и единичного номинала; "
                     "не доходность портфеля. INCOMPLETE в net не входят: исход "
                     "неизвестен."),
        "filled_stats": {
            "wins": sum(1 for n in nets if n > 0),
            "losses": sum(1 for n in nets if n < 0),
            "flat": sum(1 for n in nets if n == 0),
            "gross_sum": sum(t["gross"] for t in filled) if filled else 0.0,
            "costs_sum": sum(t["costs_total"] for t in filled) if filled else 0.0,
            "net_sum": sum(nets) if nets else 0.0,
            "net_mean_per_entry": (statistics.mean(ratios) if ratios else None),
            "net_median_per_entry": (statistics.median(ratios) if ratios else None),
        },
        "by_month": _by_month(trades, skips),
        "skip_reasons": _skip_reason_counts(skips),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")

    manifest = {
        "candidate_id": cid,
        "ticker": ticker,
        "cost_scenario": cost_id,
        "spec_hash": spec_hash,
        "code_sha": code_sha,
        "tool": TOOL,
        "status": overall,
        "cell_error": cell_error,
        "files": ["trades.csv", "skipped.csv", "signals.json", "summary.json",
                  "preflight.json", "manifest.json"],
        "event_id_count": len(event_ids),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")

    return summary


def _skip_reason_counts(skips: list) -> dict:
    out: dict = {}
    for s in skips:
        out[s["reason"]] = out.get(s["reason"], 0) + 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BATCH-15 research runner (B2)")
    ap.add_argument("--group", choices=("a", "b", "c"), required=True)
    ap.add_argument("--input-root", required=True,
                    help="каталог с <ticker>.csv (иммутабельный A10-экспорт)")
    ap.add_argument("--out", required=True, help="каталог вывода (создаётся)")
    ap.add_argument("--tickers", nargs="*", default=list(TICKERS_DEFAULT))
    ap.add_argument("--warmup-start", default=WARMUP_START_DEFAULT.isoformat())
    ap.add_argument("--eval-start", default=EVAL_START_DEFAULT.isoformat())
    ap.add_argument("--eval-end", default=EVAL_END_DEFAULT.isoformat())
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} уже существует и не пуст (immutable-каталог)")
    out.mkdir(parents=True, exist_ok=True)

    try:
        mod = __import__(f"batch15_group_{args.group}")
    except ImportError as exc:
        raise SystemExit(f"группа {args.group} недоступна: {exc}")

    a24.register_specs(mod.SPEC_HASHES)

    warmup_start = date.fromisoformat(args.warmup_start)
    eval_start = date.fromisoformat(args.eval_start)
    eval_end = date.fromisoformat(args.eval_end)

    here = Path(__file__).resolve().parent
    code_sha = {
        "batch15_common.py": _sha256_file(here / "batch15_common.py"),
        f"batch15_group_{args.group}.py": _sha256_file(here / f"batch15_group_{args.group}.py"),
        "day_candidate_execution.py": _sha256_file(here / "day_candidate_execution.py"),
        "run_batch15.py": _sha256_file(here / "run_batch15.py"),
    }

    candidates = mod.CANDIDATE_IDS
    days = _weekdays(eval_start, eval_end)

    group_summary: list = []
    errored_cells: list = []
    for ticker in args.tickers:
        input_path = Path(args.input_root) / f"{ticker}.csv"
        if not input_path.exists():
            raise SystemExit(f"нет файла {input_path}")
        by_ticker, load_issues, csv_sha = a7.load_minute_csv(str(input_path))
        if ticker not in by_ticker:
            raise SystemExit(f"тикер {ticker} отсутствует в {input_path}")
        minutes = [b for b in by_ticker[ticker] if a7.is_session_minute(b.ts)]
        issues: list = list(load_issues)
        tdata = a7.build_ticker_data(ticker, minutes, warmup_start, issues)

        preflight_common = {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "tool": TOOL,
            "group": args.group,
            "candidates": list(candidates),
            "spec_hashes": mod.SPEC_HASHES,
            "input": {"path": str(input_path), "sha256": csv_sha},
            "code_sha": code_sha,
            "params": {
                "warmup_start": warmup_start.isoformat(),
                "eval_start": eval_start.isoformat(),
                "eval_end": eval_end.isoformat(),
            },
        }

        for cid in candidates:
            for commission_bps, slippage_bps, cost_id in COST_SCENARIOS:
                costs = a7.CostParams(commission_bps, slippage_bps)
                costs.validate()
                cell_dir = out / f"{cid}__{ticker}__{cost_id}"
                try:
                    s = _run_cell(mod, tdata, cid, ticker, costs, cost_id, days,
                                  args.group, cell_dir, code_sha, preflight_common)
                except Exception as exc:  # errored cell -> ERROR, продолжаем
                    errored_cells.append(f"{cid}|{ticker}|{cost_id}: {exc!r}")
                    (cell_dir / "ERROR.json").write_text(
                        json.dumps({"error": repr(exc)}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    continue
                if s["overall_status"] == "CELL_ERROR":
                    errored_cells.append(
                        f"{cid}|{ticker}|{cost_id}: {s['cell_error']}")
                group_summary.append(s)

    agg = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": TOOL,
        "group": args.group,
        "tickers": list(args.tickers),
        "candidates": list(candidates),
        "cost_scenarios": [{"commission_bps": c, "slippage_bps": s, "id": i}
                           for c, s, i in COST_SCENARIOS],
        "code_sha": code_sha,
        "cells_total": len(group_summary),
        "errored_cells": errored_cells,
        "params": {
            "warmup_start": warmup_start.isoformat(),
            "eval_start": eval_start.isoformat(),
            "eval_end": eval_end.isoformat(),
        },
        "cells": group_summary,
    }
    (out / "_aggregate").mkdir(exist_ok=True)
    (out / "_aggregate" / "group_summary.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")

    print(f"BATCH-15 group {args.group}: cells={len(group_summary)} "
          f"errored={len(errored_cells)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
