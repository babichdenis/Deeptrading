#!/usr/bin/env python3
"""B27: bounded CLI-прогон кандидатов C1/C2 -> immutable A7/A11-ledger.

Один процесс = один тикер x один cost-сценарий; внутри ОБА кандидата C1/C2
(максимум 10 процессов ticker x cost на весь пакет). Сигналы — принятый A18
(причинный feed закрытых минут, warmup как дано); исполнение — принятый A24
adapter на существующем A7 `_walk`; accounting — A7/CostParams. Никакого нового
движка, никакого полного будущего дня для раннего сигнала, без подбора.

Выход — каталог A11-формата: manifest.json, summary.json, trades.csv,
skipped.csv, dq.csv (поля по отчёту A24/A25 ledger_schema + format A12).
Считает и пишет ТОТ ЖЕ config_hash (sort_keys, separators (",",":")), что
проверяет day_research_report.py (A11 r3), и output_sha256 по выходным файлам.

Ограничения контракта: один bounded запуск на комбинацию; eval-окно как A12
(warmup 2026-06-08, eval 2026-06-11..2026-10-08); сигналы вне окна ->
skipped.csv (outside_eval_window), в trades не входят; ноль сигналов у тикера —
легитимный результат; ретраев/подбора нет.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from day_candidate_execution import CANDIDATE_SPEC_HASH, execute_candidate_signal
from day_candidate_signals import CandidateSignalGenerator
from day_candidate_signals_fast import FastCandidateSignalGenerator
from day_strategies_core import (
    CostParams,
    SKIP_FIELDS,
    SL_ATR_MULT,
    TP_ATR_MULT,
    TRADE_FIELDS,
    is_session_minute,
    label_ts,
    load_minute_csv,
    build_ticker_data,
)

TOOL = "day_candidate_replay.py"
CANDIDATES = ("C1", "C2")

DQ_FIELDS = ["ticker", "kind", "code", "detail"]


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return str(v)
    return str(v)


def _config_hash(config_obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(config_obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_csv(path: Path, fields: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in rows:
            w.writerow([_fmt(r.get(f, "")) for f in fields])


def _day_open(tdata, day) -> str:
    mins = tdata.minutes_by_day.get(day)
    if not mins:
        return ""
    return _fmt(mins[0].open)


def _trade_row(signal: dict, rec: dict, tdata, ticker: str, config: str) -> dict:
    day = date.fromisoformat(rec["day"])
    gates = signal.get("gates") or {}
    row = {
        "ticker": ticker,
        "config": config,
        "day": rec["day"],
        "side": rec["side"],
        "decision_label": rec["decision_label"],
        "decision_bar_ts": _fmt(label_ts(day, rec["decision_label"])),
        "signal_time": rec["decision_close_time"],
        "decision_close": _fmt(gates.get("decision_close", "")),
        "day_open": _day_open(tdata, day),
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
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=TOOL)
    ap.add_argument("--input", required=True, help="1m CSV A10 (колонки ticker,ts,open,high,low,close,volume)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--candidates", default=",".join(CANDIDATES))
    ap.add_argument("--generator", choices=("a18", "fast"), default="a18",
                    help="a18 = принятый A18 (наивный, O(M^2)); fast = новый O(M), требует приёмки")
    ap.add_argument("--commission-bps", type=float, required=True)
    ap.add_argument("--slippage-bps", type=float, required=True)
    ap.add_argument("--warmup-start", type=date.fromisoformat, required=True)
    ap.add_argument("--eval-start", type=date.fromisoformat, required=True)
    ap.add_argument("--eval-end", type=date.fromisoformat, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-note", default="")
    args = ap.parse_args()

    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    unknown = [c for c in candidates if c not in CANDIDATES]
    if unknown:
        ap.error(f"неизвестные кандидаты: {unknown}")
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        ap.error(f"--out {out} уже существует и не пуст (immutable-каталог результатов)")
    out.mkdir(parents=True, exist_ok=True)

    by_ticker, load_issues, csv_sha = load_minute_csv(args.input)
    if args.ticker not in by_ticker:
        ap.error(f"тикер {args.ticker} отсутствует в {args.input} (есть: {sorted(by_ticker)})")
    raw_minutes = by_ticker[args.ticker]
    minutes = [b for b in raw_minutes if is_session_minute(b.ts)]

    issues = list(load_issues)
    tdata = build_ticker_data(args.ticker, minutes, args.warmup_start, issues)
    costs = CostParams(args.commission_bps, args.slippage_bps)
    costs.validate()

    trades: list = []
    skips: list = []
    signals: list = []
    eval_days = set()
    cur = args.eval_start
    while cur <= args.eval_end:
        eval_days.add(cur)
        cur = cur + timedelta(days=1)

    for cand in candidates:
        gen = (CandidateSignalGenerator if args.generator == "a18"
               else FastCandidateSignalGenerator)(cand, args.ticker, warmup_start=args.warmup_start)
        for b in minutes:
            gen.feed(b)
        saw_days = set()
        for ev in gen.events:
            eday = date.fromisoformat(ev["day"])
            saw_days.add(eday)
            if eday not in eval_days:
                skips.append({"ticker": args.ticker, "config": cand, "day": ev["day"],
                              "reason": "outside_eval_window",
                              "detail": f"event {ev['type']} вне eval-окна "
                                        f"{args.eval_start.isoformat()}..{args.eval_end.isoformat()}"})
                continue
            if ev["type"] != "signal":
                skips.append({"ticker": args.ticker, "config": cand, "day": ev["day"],
                              "reason": ev.get("reason", "skip"), "detail": ev.get("detail", "")})
                continue
            signals.append(ev)
            rec = execute_candidate_signal(ev, tdata, costs,
                                           expected_spec_hash=CANDIDATE_SPEC_HASH)
            if rec["status"] == "REJECT":
                (out / "rejected_signal.json").write_text(
                    json.dumps({"signal": ev, "result": rec}, ensure_ascii=False,
                               indent=2, allow_nan=False), encoding="utf-8")
                raise ValueError("candidate_signal_rejected: " + rec["incomplete_reason"])
            if rec["status"] in ("FILLED", "INCOMPLETE"):
                trades.append(_trade_row(ev, rec, tdata, args.ticker, cand))
            else:
                skips.append({"ticker": args.ticker, "config": cand, "day": ev["day"],
                              "reason": rec["incomplete_reason"], "detail": rec["detail"]})
        for eday in sorted(eval_days - saw_days):
            skips.append({"ticker": args.ticker, "config": cand, "day": eday.isoformat(),
                          "reason": "insufficient_context",
                          "detail": "нет терминального события (обрыв префикса/окна до 14:10)"})

    keyf = lambda r: (r.get("ticker", ""), r.get("config", ""),
                      r.get("day", ""), r.get("entry_ts", ""), r.get("side", ""))
    trades.sort(key=keyf)
    skips.sort(key=keyf)
    issues.sort(key=lambda dq: (dq.ticker, dq.kind, dq.code, dq.detail))

    _write_csv(out / "trades.csv", TRADE_FIELDS, trades)
    _write_csv(out / "skipped.csv", SKIP_FIELDS, skips)
    _write_csv(out / "dq.csv", DQ_FIELDS, [{"ticker": d.ticker, "kind": d.kind,
                                            "code": d.code, "detail": d.detail} for d in issues])

    filled_n = sum(1 for t in trades if t["status"] == "FILLED")
    incomplete_n = len(trades) - filled_n
    dq_errors = sum(1 for d in issues if d.kind == "error")
    status_parts = []
    if incomplete_n:
        status_parts.append("INCOMPLETE_TRADES")
    if dq_errors:
        status_parts.append("DQ_ERRORS")
    overall_status = "+".join(status_parts) if status_parts else "OK"

    counts = {
        "trades_total": len(trades), "filled": filled_n,
        "incomplete": incomplete_n, "skipped": len(skips),
        "dq_issues": len(issues), "dq_errors": dq_errors,
    }
    summary = {
        "gross_only": costs.gross_only,
        "gross_only_note": "",
        "overall_status": overall_status,
        "overall_status_note": ("OK = незавершённых путей и DQ-ошибок нет; "
                                "INCOMPLETE_TRADES = есть INCOMPLETE (exit/net неизвестны); "
                                "DQ_ERRORS = есть error-записи в dq.csv"),
        "params": {
            "warmup_start": args.warmup_start.isoformat(),
            "eval_start": args.eval_start.isoformat(),
            "eval_end": args.eval_end.isoformat(),
            "commission_bps": args.commission_bps,
            "slippage_bps": args.slippage_bps,
            "configs": candidates,
            "generator": args.generator,
        },
        "counts": counts,
        "by_group": [],
        "net_verdict": "development_unvalidated; profitability UNKNOWN; not OOS",
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8")
    (out / "signals.json").write_text(
        json.dumps(signals, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")

    configs_detail = []
    for cand in candidates:
        if cand == "C1":
            detail = {"config": cand, "candidate": cand, "or_labels": ["10:00", "10:10", "10:20"],
                      "scan": "10:30-14:00", "sl_atr_mult": SL_ATR_MULT, "tp_atr_mult": TP_ATR_MULT}
        else:
            detail = {"config": cand, "candidate": cand, "scan": "11:00-14:00",
                      "slope_minutes": 60, "sl_atr_mult": SL_ATR_MULT, "tp_atr_mult": TP_ATR_MULT}
        configs_detail.append(detail)

    config_obj = {
        "generator": args.generator,
        "warmup_start": args.warmup_start.isoformat(),
        "eval_start": args.eval_start.isoformat(),
        "eval_end": args.eval_end.isoformat(),
        "commission_bps": args.commission_bps,
        "slippage_bps": args.slippage_bps,
        "configs": candidates,
        "configs_detail": configs_detail,
        "spec_hash": CANDIDATE_SPEC_HASH,
    }
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": TOOL,
        "canon": "B27 BRANCH DISPATCH 2026-10-10; A17 canon 2a1ecab0…; A12 window/costs",
        "data_note": args.data_note,
        "inputs": [{"path": str(args.input), "sha256": csv_sha}],
        "code_sha256": {
            "day_strategies_core.py": _sha256_file(Path(__file__).resolve().parent / "day_strategies_core.py"),
            "day_candidate_signals.py": _sha256_file(Path(__file__).resolve().parent / "day_candidate_signals.py"),
            "day_candidate_signals_fast.py": _sha256_file(Path(__file__).resolve().parent / "day_candidate_signals_fast.py"),
            "day_candidate_execution.py": _sha256_file(Path(__file__).resolve().parent / "day_candidate_execution.py"),
            "day_candidate_replay.py": _sha256_file(Path(__file__).resolve()),
        },
        "generator": args.generator,
        "config": config_obj,
        "config_hash": _config_hash(config_obj),
        "costs_semantics": ("slippage: adverse fill против позиции; commission = notional(fill)"
                            "*bps/1e4 на входе и выходе; net = fill_delta - ce - cx; "
                            "costs_total = ce + cx + (gross - fill_delta); net = gross - costs_total "
                            "по построению."),
        "counts": counts,
        "overall_status": overall_status,
        "net_verdict": summary["net_verdict"],
        "output_sha256": {
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

    print(json.dumps({
        "run_id": out.name, "ticker": args.ticker, "configs": candidates,
        "counts": counts, "overall_status": overall_status,
        "manifest_sha256": _sha256_file(out / "manifest.json"),
    }, ensure_ascii=False, indent=2))
    return 0


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
