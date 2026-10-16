#!/usr/bin/env python3
"""C3_TREND60_V1 runner: один фиксированный offline-прогон нового кандидата.

Single: один тикер x один cost-сценарий за процесс (bounded), вход — уже
сохранённый 1m CSV (A10-экспорт из БД), читается существующим a7-loader
`load_minute_csv`. Прямого биржевого API нет. Выход — immutable A11-формата:
preflight.json (spec/hash входа/версия Python ДО расчёта), manifest.json,
summary.json, trades.csv, skipped.csv, signals.json, dq.csv.

Aggregate: читает набор run-каталогов и пишет combined_summary.json +
summary_by_ticker_month_cost.csv. Сырой ценовой net РАЗНЫХ инструментов НЕ
суммируется: по инструменту показываются net/entry_base (единичный номинал), а
кросс-инструментный агрегат назван распределением сделок, не доходностью портфеля.

Сигналы: `intraday_trend_candidate.evaluate_trend_day`; исполнение — существующий
A24 `execute_candidate_signal` через общее ядро research_execution; costs — A7
CostParams. Никакого нового движка/подбора.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from day_candidate_execution import execute_candidate_signal
from day_strategies_core import (
    CostParams,
    SKIP_FIELDS,
    TRADE_FIELDS,
    build_ticker_data,
    eval_days,
    is_session_minute,
    label_ts,
    load_minute_csv,
)
from intraday_trend_candidate import (
    CANDIDATE_ID,
    DECISION_LABEL,
    TREND_SPEC_HASH,
    evaluate_trend_day,
)

TOOL = "run_intraday_trend.py"
DQ_FIELDS = ["ticker", "kind", "code", "detail"]
CONFIG = CANDIDATE_ID
NET_VERDICT = "development_unvalidated; profitability UNKNOWN; not OOS"


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return str(v)
    return str(v)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_hash(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_csv(path: Path, fields: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for r in rows:
            w.writerow([_fmt(r.get(f, "")) for f in fields])


def _weekdays(start: date, end: date) -> list:
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _decision_bar_ts(day: date) -> str:
    return label_ts(day, DECISION_LABEL).isoformat()


def _hour_open(tdata, day: date):
    bar = tdata.bar_by_day_label.get((day, 600))
    return _fmt(bar.open) if bar is not None else ""


def _trade_row(ev: dict, rec: dict, tdata, ticker: str) -> dict:
    day = date.fromisoformat(rec["day"])
    gates = ev.get("gates") or {}
    return {
        "ticker": ticker,
        "config": CONFIG,
        "day": rec["day"],
        "side": rec["side"],
        "decision_label": rec["decision_label"],
        "decision_bar_ts": _decision_bar_ts(day),
        "signal_time": rec["decision_close_time"],
        "decision_close": _fmt(gates.get("decision_close", "")),
        "day_open": _hour_open(tdata, day),
        "half_ret": "", "gap": "", "cont": "", "threshold": "",
        "atr": rec["atr"],
        "sl_level": rec["sl_level"], "tp_level": rec["tp_level"],
        "entry_ts": rec["entry_ts"], "entry_base": rec["entry_base"],
        "entry_fill": rec["entry_fill"], "slip_entry": rec["slip_entry"],
        "commission_entry": rec["commission_entry"],
        "exit_ts": rec["exit_ts"], "exit_base": rec["exit_base"],
        "exit_fill": rec["exit_fill"], "slip_exit": rec["slip_exit"],
        "commission_exit": rec["commission_exit"], "exit_reason": rec["exit_reason"],
        "gross": rec["gross"], "costs_total": rec["costs_total"], "net": rec["net"],
        "status": rec["status"], "incomplete_reason": rec["incomplete_reason"],
    }


def _summary_counts(trades: list, skips: list, issues: list) -> dict:
    filled = [t for t in trades if t["status"] == "FILLED"]
    incomplete = [t for t in trades if t["status"] == "INCOMPLETE"]
    insuff = [s for s in skips if s["reason"] == "incomplete_hour"
              or s["reason"].startswith("atr_")]
    dq_errors = sum(1 for d in issues if d.kind == "error")
    return {
        "days_evaluated": None,  # set by caller
        "signals": None,
        "trades_total": len(trades),
        "filled": len(filled),
        "incomplete": len(incomplete),
        "skipped": len(skips),
        "insufficient_context": len(insuff),
        "dq_issues": len(issues),
        "dq_errors": dq_errors,
    }


def _blank_month(month: str) -> dict:
    return {"month": month, "filled": 0, "incomplete": 0, "long": 0, "short": 0,
            "wins": 0, "losses": 0, "gross_sum": 0.0, "costs_sum": 0.0,
            "net_sum": 0.0, "unknown": 0, "no_signal": 0}


def _is_unknown(reason: str) -> bool:
    return reason == "incomplete_hour" or reason.startswith("atr_")


def _by_month(trades: list, skips: list) -> list:
    bucket: dict = {}
    for t in trades:
        row = bucket.setdefault(t["day"][:7], _blank_month(t["day"][:7]))
        if t["status"] == "FILLED":
            row["filled"] += 1
            row["long" if t["side"] == "long" else "short"] += 1
            if t["net"] > 0:
                row["wins"] += 1
            elif t["net"] < 0:
                row["losses"] += 1
            row["gross_sum"] += t["gross"]
            row["costs_sum"] += t["costs_total"]
            row["net_sum"] += t["net"]
        else:
            row["incomplete"] += 1
    for s in skips:
        row = bucket.setdefault(s["day"][:7], _blank_month(s["day"][:7]))
        if _is_unknown(s["reason"]):
            row["unknown"] += 1
        elif s["reason"] == "no_signal":
            row["no_signal"] += 1
    return [bucket[m] for m in sorted(bucket)]


def _run_single(args) -> int:
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} уже существует и не пуст (immutable-каталог)")
    out.mkdir(parents=True, exist_ok=True)

    if args.spec_hash is not None and args.spec_hash != TREND_SPEC_HASH:
        raise SystemExit(f"--spec-hash {args.spec_hash} != {TREND_SPEC_HASH}")

    by_ticker, load_issues, csv_sha = load_minute_csv(args.input)
    if args.ticker not in by_ticker:
        raise SystemExit(f"тикер {args.ticker} отсутствует в {args.input} "
                         f"(есть: {sorted(by_ticker)})")
    minutes = [b for b in by_ticker[args.ticker] if is_session_minute(b.ts)]

    issues = list(load_issues)
    tdata = build_ticker_data(args.ticker, minutes, args.warmup_start, issues)
    costs = CostParams(args.commission_bps, args.slippage_bps)
    costs.validate()

    preflight = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": TOOL,
        "candidate": CONFIG,
        "spec_hash": TREND_SPEC_HASH,
        "input": {"path": str(args.input), "sha256": csv_sha},
        "params": {
            "ticker": args.ticker,
            "warmup_start": args.warmup_start.isoformat(),
            "eval_start": args.eval_start.isoformat(),
            "eval_end": args.eval_end.isoformat(),
            "commission_bps": args.commission_bps,
            "slippage_bps": args.slippage_bps,
        },
    }
    (out / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")

    trades: list = []
    skips: list = []
    signals: list = []
    days = _weekdays(args.eval_start, args.eval_end)
    window_days = set(eval_days(tdata, args.eval_start, args.eval_end))

    for day in days:
        ev = evaluate_trend_day(tdata, day, spec_hash=TREND_SPEC_HASH)
        if ev["type"] == "signal":
            signals.append(ev)
            rec = execute_candidate_signal(ev, tdata, costs,
                                           expected_spec_hash=TREND_SPEC_HASH)
            if rec["status"] == "REJECT":
                (out / "rejected_signal.json").write_text(
                    json.dumps({"signal": ev, "result": rec}, ensure_ascii=False,
                               indent=2, allow_nan=False), encoding="utf-8")
                raise SystemExit("candidate_signal_rejected: " + rec["incomplete_reason"])
            if rec["status"] in ("FILLED", "INCOMPLETE"):
                trades.append(_trade_row(ev, rec, tdata, args.ticker))
            else:
                skips.append({"ticker": args.ticker, "config": CONFIG, "day": ev["day"],
                              "reason": rec["incomplete_reason"], "detail": rec["detail"]})
        else:
            skips.append({"ticker": args.ticker, "config": CONFIG, "day": ev["day"],
                          "reason": ev.get("reason", ev["type"]),
                          "detail": ev.get("detail", "")})

    keyf = lambda r: (r.get("ticker", ""), r.get("config", ""), r.get("day", ""),
                      r.get("entry_ts", ""), r.get("side", ""))
    trades.sort(key=keyf)
    skips.sort(key=keyf)
    issues.sort(key=lambda dq: (dq.ticker, dq.kind, dq.code, dq.detail))

    _write_csv(out / "trades.csv", TRADE_FIELDS, trades)
    _write_csv(out / "skipped.csv", SKIP_FIELDS, skips)
    _write_csv(out / "dq.csv", DQ_FIELDS,
               [{"ticker": d.ticker, "kind": d.kind, "code": d.code, "detail": d.detail}
                for d in issues])

    counts = _summary_counts(trades, skips, issues)
    counts["days_evaluated"] = len(days)
    counts["days_with_session_data"] = len(window_days)
    counts["signals"] = len(signals)

    filled = [t for t in trades if t["status"] == "FILLED"]
    nets = [t["net"] for t in filled]
    status_parts = []
    if counts["incomplete"]:
        status_parts.append("INCOMPLETE_TRADES")
    if counts["dq_errors"]:
        status_parts.append("DQ_ERRORS")
    overall_status = "+".join(status_parts) if status_parts else "OK"

    summary = {
        "candidate": CONFIG,
        "spec_hash": TREND_SPEC_HASH,
        "gross_only": costs.gross_only,
        "overall_status": overall_status,
        "note": ("net_sum ниже — для ОДНОГО инструмента и единичного номинала "
                 "(net/entry_base по сделкам в trades.csv); это не доходность портфеля. "
                 "Незакрытые (INCOMPLETE) в net не входят: исход неизвестен."),
        "params": {
            "ticker": args.ticker,
            "warmup_start": args.warmup_start.isoformat(),
            "eval_start": args.eval_start.isoformat(),
            "eval_end": args.eval_end.isoformat(),
            "commission_bps": args.commission_bps,
            "slippage_bps": args.slippage_bps,
        },
        "counts": counts,
        "filled_stats": {
            "wins": sum(1 for n in nets if n > 0),
            "losses": sum(1 for n in nets if n < 0),
            "flat": sum(1 for n in nets if n == 0),
            "gross_sum": sum(t["gross"] for t in filled) if filled else 0.0,
            "costs_sum": sum(t["costs_total"] for t in filled) if filled else 0.0,
            "net_sum": sum(nets) if nets else 0.0,
        },
        "by_month": _by_month(trades, skips),
        "skip_reasons": _skip_reason_counts(skips),
        "net_verdict": NET_VERDICT,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str,
                   allow_nan=False) + "\n", encoding="utf-8")
    (out / "signals.json").write_text(
        json.dumps(signals, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")

    config_obj = {
        "candidate": CONFIG,
        "spec_hash": TREND_SPEC_HASH,
        "ticker": args.ticker,
        "warmup_start": args.warmup_start.isoformat(),
        "eval_start": args.eval_start.isoformat(),
        "eval_end": args.eval_end.isoformat(),
        "commission_bps": args.commission_bps,
        "slippage_bps": args.slippage_bps,
    }
    here = Path(__file__).resolve().parent
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": TOOL,
        "canon": "WORKLINE-2H-20261011 Builder-2 C3_TREND60_V1",
        "data_note": args.data_note,
        "inputs": [{"path": str(args.input), "sha256": csv_sha}],
        "code_sha256": {
            "day_strategies_core.py": _sha256_file(here / "day_strategies_core.py"),
            "day_candidate_execution.py": _sha256_file(here / "day_candidate_execution.py"),
            "intraday_trend_candidate.py": _sha256_file(here / "intraday_trend_candidate.py"),
            "run_intraday_trend.py": _sha256_file(Path(__file__).resolve()),
        },
        "spec_hash": TREND_SPEC_HASH,
        "config": config_obj,
        "config_hash": _config_hash(config_obj),
        "costs_semantics": ("slippage: adverse fill против позиции; commission = "
                            "notional(fill)*bps/1e4 на входе и выходе; net = fill_delta "
                            "- ce - cx; net = gross - costs_total по построению."),
        "counts": counts,
        "overall_status": overall_status,
        "net_verdict": NET_VERDICT,
        "output_sha256": {
            "preflight.json": _sha256_file(out / "preflight.json"),
            "trades.csv": _sha256_file(out / "trades.csv"),
            "skipped.csv": _sha256_file(out / "skipped.csv"),
            "dq.csv": _sha256_file(out / "dq.csv"),
            "summary.json": _sha256_file(out / "summary.json"),
            "signals.json": _sha256_file(out / "signals.json"),
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")

    print(json.dumps({"run_id": out.name, "ticker": args.ticker, "counts": counts,
                      "overall_status": overall_status,
                      "manifest_sha256": _sha256_file(out / "manifest.json")},
                     ensure_ascii=False, indent=2))
    return 0


def _skip_reason_counts(skips: list) -> dict:
    out: dict = {}
    for s in skips:
        out[s["reason"]] = out.get(s["reason"], 0) + 1
    return dict(sorted(out.items()))


def _read_run(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    with (run_dir / "trades.csv").open(encoding="utf-8") as fh:
        trades = list(csv.DictReader(fh))
    with (run_dir / "skipped.csv").open(encoding="utf-8") as fh:
        skips = list(csv.DictReader(fh))
    return {"dir": str(run_dir), "manifest": manifest,
            "params": manifest["config"], "trades": trades, "skips": skips}


def _run_aggregate(args) -> int:
    root = Path(args.runs_root)
    run_dirs = sorted(d for d in root.iterdir()
                      if d.is_dir() and (d / "manifest.json").exists())
    if not run_dirs:
        raise SystemExit(f"в {root} нет run-каталогов с manifest.json")
    runs = [_read_run(d) for d in run_dirs]

    rows: list = []
    for run in runs:
        cfg = run["params"]
        ticker = cfg["ticker"]
        cost = f"{cfg['commission_bps']}/{cfg['slippage_bps']}bps"
        buckets: dict = {}

        def _row_for(month: str) -> dict:
            return buckets.setdefault(month, {"ticker": ticker, "costs": cost,
                                              "month": month, "filled": 0, "incomplete": 0,
                                              "long": 0, "short": 0, "wins": 0, "losses": 0,
                                              "gross_sum": 0.0, "costs_sum": 0.0,
                                              "net_sum": 0.0, "unknown": 0, "no_signal": 0})

        for t in run["trades"]:
            row = _row_for(t["day"][:7])
            if t["status"] == "FILLED":
                row["filled"] += 1
                row["long" if t["side"] == "long" else "short"] += 1
                net = float(t["net"])
                if net > 0:
                    row["wins"] += 1
                elif net < 0:
                    row["losses"] += 1
                row["gross_sum"] += float(t["gross"])
                row["costs_sum"] += float(t["costs_total"])
                row["net_sum"] += net
            else:
                row["incomplete"] += 1
        for s in run["skips"]:
            row = _row_for(s["day"][:7])
            if _is_unknown(s["reason"]):
                row["unknown"] += 1
            elif s["reason"] == "no_signal":
                row["no_signal"] += 1
        for month in sorted(buckets):
            rows.append(buckets[month])
    rows.sort(key=lambda r: (r["ticker"], r["costs"], r["month"]))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = ["ticker", "costs", "month", "filled", "incomplete", "long", "short",
              "wins", "losses", "gross_sum", "costs_sum", "net_sum", "unknown",
              "no_signal"]
    _write_csv(out_dir / "summary_by_ticker_month_cost.csv", fields, rows)

    distribution = {"by_cost": {}, "by_ticker": {}}
    for run in runs:
        cfg = run["params"]
        cost = f"{cfg['commission_bps']}/{cfg['slippage_bps']}bps"
        filled = [t for t in run["trades"] if t["status"] == "FILLED"]
        dist = distribution["by_cost"].setdefault(
            cost, {"trades_filled": 0, "wins": 0, "losses": 0, "incomplete": 0})
        dist["trades_filled"] += len(filled)
        dist["wins"] += sum(1 for t in filled if float(t["net"]) > 0)
        dist["losses"] += sum(1 for t in filled if float(t["net"]) < 0)
        dist["incomplete"] += sum(1 for t in run["trades"] if t["status"] == "INCOMPLETE")
        distribution["by_ticker"].setdefault(cfg["ticker"], {"runs": 0})["runs"] += 1

    combined = {
        "candidate": CONFIG,
        "spec_hash": TREND_SPEC_HASH,
        "note": ("Это РАСПРЕДЕЛЕНИЕ СДЕЛОК, а не доходность портфеля: сырой ценовой "
                 "net разных инструментов не суммируется. per-instrument net/entry_base "
                 "смотри trades.csv соответствующего run; два cost-сценария (5/2, 10/5) "
                 "НЕ независимые наблюдения."),
        "net_verdict": NET_VERDICT,
        "runs": [{"dir": r["dir"], "params": r["params"]} for r in runs],
        "trade_distribution": distribution,
    }
    (out_dir / "combined_summary.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(json.dumps({"runs": len(runs), "rows": len(rows),
                      "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=TOOL)
    sub = ap.add_subparsers(dest="mode")

    p = sub.add_parser("single", help="один тикер x один cost-сценарий")
    p.add_argument("--input", required=True, help="1m CSV (a7 columns)")
    p.add_argument("--ticker", required=True)
    p.add_argument("--commission-bps", type=float, required=True)
    p.add_argument("--slippage-bps", type=float, required=True)
    p.add_argument("--warmup-start", type=date.fromisoformat, required=True)
    p.add_argument("--eval-start", type=date.fromisoformat, required=True)
    p.add_argument("--eval-end", type=date.fromisoformat, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--spec-hash", default=None)
    p.add_argument("--data-note", default="")

    p2 = sub.add_parser("aggregate", help="сводка по набору run-каталогов")
    p2.add_argument("--runs-root", required=True)
    p2.add_argument("--out-dir", required=True)

    args = ap.parse_args(argv)
    if args.mode == "single":
        return _run_single(args)
    if args.mode == "aggregate":
        return _run_aggregate(args)
    ap.error("укажите режим: single | aggregate")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
