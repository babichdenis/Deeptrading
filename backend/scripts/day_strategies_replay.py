#!/usr/bin/env python3
"""A7 CLI: offline-прогон дневных стратегий S1/S2 по локальным 1m-CSV (DISPATCH A7).

Research-only: без сети, без БД, без импорта app.*. Вход — явно переданные файлы
1m-свечей (ticker, ts UTC aware, open/high/low/close/volume); тёплый период и
оцениваемый период задаются датами Europe/Moscow. Издержки — обязательные
параметры CLI (bps/сторону, конечные >= 0); нулевые издержки допустимы только с
явной пометкой gross_only в summary/manifest (это НЕ положительный net-вердикт).

Пример:
  python backend/scripts/day_strategies_replay.py \
      --input backend/reports/.../inputs/ex01.csv \
      --warmup-start 2026-09-07 --eval-start 2026-09-08 --eval-end 2026-09-08 \
      --commission-bps 0 --slippage-bps 0 \
      --configs S1-A --out <каталог результатов> \
      --data-note "SYNTHETIC: синтетический пример, не реальные series"

Выход (--out; каталог создаётся, существующий непустой запрещён):
  trades.csv, skipped.csv, dq.csv, summary.json, manifest.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from day_strategies_core import (
    CONFIGS,
    SKIP_FIELDS,
    TRADE_FIELDS,
    CostParams,
    build_ticker_data,
    load_minute_csv,
    run_ticker,
    summarize,
)

DQ_FIELDS = ("ticker", "kind", "code", "detail")

COSTS_SEMANTICS = (
    "slippage: adverse fill против позиции (buy base*(1+s), sell base*(1-s)), "
    "slippage_bps < 10000 (fill обязан быть строго положительным); "
    "commission = notional(fill)*bps/1e4 на входе и на выходе; "
    "fill_delta = side*(exit_fill - entry_fill); "
    "net = fill_delta - commission_entry - commission_exit; "
    "costs_total = commission_entry + commission_exit + (gross - fill_delta); "
    "тождество net = gross - costs_total выполняется по построению; "
    "округление fill к тику не применяется (tick_size удалён в r2)."
)

NET_VERDICT = ("NOT_ASSIGNED: WF/OOS и финальный holdout не выполнялись; результат — "
               "только реализация контракта A7, прибыльность UNKNOWN.")


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается дата YYYY-MM-DD: {s!r}") from None


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _write_csv(path: Path, fields, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k, "")) for k in fields})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A7 offline replay S1/S2 (research-only)")
    ap.add_argument("--input", action="append", required=True,
                    help="1m-CSV (ticker,ts,open,high,low,close,volume); можно повторять")
    ap.add_argument("--warmup-start", type=_parse_date, required=True,
                    help="начало тёплого периода ATR (дата Europe/Moscow, включительно)")
    ap.add_argument("--eval-start", type=_parse_date, required=True,
                    help="первый оцениваемый день (строго позже warmup-start)")
    ap.add_argument("--eval-end", type=_parse_date, required=True)
    ap.add_argument("--commission-bps", type=float, required=True,
                    help="комиссия, bps на сторону (конечное, >=0)")
    ap.add_argument("--slippage-bps", type=float, required=True,
                    help="проскальзывание, bps на сторону (конечное, >=0, <10000)")
    ap.add_argument("--configs", nargs="+", default=[c.config for c in CONFIGS],
                    choices=[c.config for c in CONFIGS])
    ap.add_argument("--out", type=Path, required=True,
                    help="каталог результатов (создаётся; непустой существующий запрещён)")
    ap.add_argument("--data-note", default="",
                    help="пометка о данных (напр. SYNTHETIC), попадает в manifest")
    args = ap.parse_args(argv)

    if args.eval_end < args.eval_start:
        ap.error("eval-end < eval-start")
    if args.eval_start <= args.warmup_start:
        ap.error("eval-start должен быть строго позже warmup-start "
                 "(торговых дней внутри тёплого периода нет)")

    try:
        costs = CostParams(args.commission_bps, args.slippage_bps)
        costs.validate()
    except ValueError as exc:
        ap.error(str(exc))

    cfg_set = set(args.configs)
    cfgs = [c for c in CONFIGS if c.config in cfg_set]

    inputs = []
    by_ticker = {}
    issues = []
    for p in args.input:
        tick, dq, sha = load_minute_csv(p)
        inputs.append({"path": str(p), "sha256": sha})
        issues.extend(dq)
        for t, rows in tick.items():
            if t in by_ticker:
                raise SystemExit(f"ticker {t!r} встречается более чем в одном файле — "
                                 f"объединение не поддержано")
            by_ticker[t] = rows
    if not by_ticker:
        raise SystemExit("нет валидных строк ни в одном входном файле")
    issues.sort(key=lambda d: (d.ticker, d.kind, d.code, d.detail))

    all_trades, all_skips = [], []
    for t in sorted(by_ticker):
        tdata = build_ticker_data(t, by_ticker[t], args.warmup_start, issues,
                                  warmup_end=args.eval_start)
        tr, sk = run_ticker(tdata, cfgs, args.eval_start, args.eval_end, costs)
        all_trades.extend(tr)
        all_skips.extend(sk)

    out = args.out
    if out.exists() and any(out.iterdir()):
        ap.error(f"--out {out} уже существует и не пуст (immutable-каталог результатов)")
    out.mkdir(parents=True, exist_ok=True)

    def key(r):
        return (r["ticker"], r["config"], r["day"], r.get("side", ""))

    _write_csv(out / "trades.csv", TRADE_FIELDS, sorted(all_trades, key=key))
    _write_csv(out / "skipped.csv", SKIP_FIELDS, sorted(all_skips, key=key))
    _write_csv(out / "dq.csv", DQ_FIELDS, [asdict(d) for d in issues])

    filled_n = sum(1 for t in all_trades if t["status"] == "FILLED")
    incomplete_n = len(all_trades) - filled_n
    dq_errors = sum(1 for d in issues if d.kind == "error")
    status_parts = []
    if incomplete_n:
        status_parts.append("INCOMPLETE_TRADES")
    if dq_errors:
        status_parts.append("DQ_ERRORS")
    overall_status = "+".join(status_parts) if status_parts else "OK"

    summary = {
        "gross_only": costs.gross_only,
        "gross_only_note": ("нулевые издержки: результаты только gross; это НЕ "
                            "положительный net-вердикт" if costs.gross_only else ""),
        "overall_status": overall_status,
        "overall_status_note": ("OK = незавершённых путей и DQ-ошибок нет; "
                                "INCOMPLETE_TRADES = есть INCOMPLETE (exit/net неизвестны, "
                                "в by_group не входят); DQ_ERRORS = есть error-записи в dq.csv"),
        "params": {
            "warmup_start": args.warmup_start.isoformat(),
            "eval_start": args.eval_start.isoformat(),
            "eval_end": args.eval_end.isoformat(),
            "commission_bps": args.commission_bps,
            "slippage_bps": args.slippage_bps,
            "configs": [c.config for c in cfgs],
        },
        "counts": {"trades_total": len(all_trades), "filled": filled_n,
                   "incomplete": incomplete_n, "skipped": len(all_skips),
                   "dq_issues": len(issues), "dq_errors": dq_errors},
        "by_group": summarize(all_trades),
        "net_verdict": NET_VERDICT,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8")

    core_path = Path(__file__).resolve().parent / "day_strategies_core.py"
    config_obj = {
        "warmup_start": args.warmup_start.isoformat(),
        "eval_start": args.eval_start.isoformat(),
        "eval_end": args.eval_end.isoformat(),
        "commission_bps": args.commission_bps,
        "slippage_bps": args.slippage_bps,
        "configs": [c.config for c in cfgs],
        "configs_detail": [asdict(c) for c in cfgs],
    }
    config_hash = hashlib.sha256(
        json.dumps(config_obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": "day_strategies_replay.py",
        "canon": "docs/results/ARCH_20261008_B2_A4.md §4–§6, §10; DISPATCH A7 2026-10-09 (chat.md)",
        "data_note": args.data_note,
        "inputs": inputs,
        "code_sha256": {
            "day_strategies_core.py": _sha256_file(core_path),
            "day_strategies_replay.py": _sha256_file(Path(__file__).resolve()),
        },
        "config": config_obj,
        "config_hash": config_hash,
        "costs_semantics": COSTS_SEMANTICS,
        "counts": {"trades": len(all_trades), "filled": filled_n,
                   "incomplete": incomplete_n, "skipped": len(all_skips),
                   "dq_issues": len(issues), "dq_errors": dq_errors},
        "overall_status": overall_status,
        "net_verdict": NET_VERDICT,
    }
    manifest["output_sha256"] = {
        name: _sha256_file(out / name)
        for name in ("trades.csv", "skipped.csv", "dq.csv", "summary.json")
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8")

    print(f"A7 replay done: {out} (trades={len(all_trades)}, skipped={len(all_skips)}, "
          f"dq={len(issues)})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OverflowError) as exc:
        print(f"A7 rejected: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2)
