#!/usr/bin/env python3
"""BATCH-15 summary (B3). Собирает все 15 кандидатов из артефактов групп a/b/c.

Читает <input>/<run_id>/{b1,b2,b3}/<run_id>/<cid>__<ticker>__<cost_id>/summary.json
(структура runner'а B2) и агрегирует в results.csv (120 строк candidate×ticker×cost)
+ короткий RESULTS.md. Денежные PnL разных акций НЕ складываются: net/entry_base —
для единичного номинала одного инструмента. Cost-сценарии — пары, не независимые
наблюдения. INCOMPLETE без числового net; unknown не скрывается.

Запуск:
    python backend/scripts/summarize_batch15.py --root backend/reports/architect_20261011/batch15 \
        --out backend/reports/architect_20261011/batch15/_summary
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

GROUP_DIRS = {"b1": "a", "b2": "b", "b3": "c"}
CANDIDATE_IDS = (
    "B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09", "B10",
    "B11", "B12", "B13", "B14", "B15",
)
TICKERS = ("GAZP", "LKOH", "SFIN", "VKCO")
COST_IDS = ("c5_s2", "c10_s5")

RESULTS_FIELDS = (
    "candidate_id", "group", "ticker", "cost_scenario",
    "days_evaluated", "signals", "FILLED", "INCOMPLETE", "SKIP",
    "insufficient_context", "REJECT", "wins", "losses", "flat",
    "gross_sum", "costs_sum", "net_sum", "net_mean_per_entry",
    "net_median_per_entry", "overall_status", "cell_error", "spec_hash",
)


def _load_cell(root: Path, cid: str, ticker: str, cost_id: str) -> tuple[dict | None, str]:
    for gd in GROUP_DIRS:
        for run_dir in sorted((root / gd).glob("*")) if (root / gd).exists() else []:
            cell = run_dir / f"{cid}__{ticker}__{cost_id}"
            summary = cell / "summary.json"
            if summary.exists():
                try:
                    return json.loads(summary.read_text(encoding="utf-8")), gd
                except (json.JSONDecodeError, OSError) as exc:
                    return None, f"{gd}: ошибка чтения summary: {exc}"
            if (cell / "ERROR.json").exists():
                err = (cell / "ERROR.json").read_text(encoding="utf-8")
                return {"cell_error": f"ERROR: {err[:200]}", "overall_status": "ERROR"}, gd
    return None, "нет артефакта"


def _cell_row(summary: dict | None, group: str, cid: str, ticker: str,
              cost_id: str) -> dict:
    if summary is None:
        return {"candidate_id": cid, "group": group, "ticker": ticker,
                "cost_scenario": cost_id, "overall_status": "MISSING",
                "cell_error": f"нет артефакта {cid}|{ticker}|{cost_id}"}
    fs = summary.get("filled_stats", {})
    return {
        "candidate_id": summary.get("candidate_id", cid),
        "group": group,
        "ticker": summary.get("ticker", ticker),
        "cost_scenario": summary.get("cost_scenario", cost_id),
        "days_evaluated": summary.get("days_evaluated", ""),
        "signals": summary.get("signals", ""),
        "FILLED": summary.get("FILLED", ""),
        "INCOMPLETE": summary.get("INCOMPLETE", ""),
        "SKIP": summary.get("SKIP", ""),
        "insufficient_context": summary.get("insufficient_context", ""),
        "REJECT": summary.get("REJECT", ""),
        "wins": fs.get("wins", ""),
        "losses": fs.get("losses", ""),
        "flat": fs.get("flat", ""),
        "gross_sum": fs.get("gross_sum", ""),
        "costs_sum": fs.get("costs_sum", ""),
        "net_sum": fs.get("net_sum", ""),
        "net_mean_per_entry": fs.get("net_mean_per_entry", ""),
        "net_median_per_entry": fs.get("net_median_per_entry", ""),
        "overall_status": summary.get("overall_status", ""),
        "cell_error": summary.get("cell_error", ""),
        "spec_hash": summary.get("spec_hash", ""),
    }


def _verdict(rows: list[dict]) -> str:
    """Короткий вердикт по кандидату: положительный/отрицательный/недостаточно/ERROR."""
    ok = []
    for r in rows:
        if r.get("overall_status") in ("ERROR", "MISSING"):
            continue
        mean = r.get("net_mean_per_entry")
        if mean is None and isinstance(r.get("filled_stats"), dict):
            mean = r["filled_stats"].get("net_mean_per_entry")
        if isinstance(mean, (int, float)):
            ok.append(mean)
    if not ok:
        if any(r.get("overall_status") == "ERROR" for r in rows):
            return "ERROR"
        if any(r.get("overall_status") == "MISSING" for r in rows):
            return "MISSING"
        return "insufficient_data"
    avg = sum(ok) / len(ok)
    if avg > 0:
        return "positive_on_known"
    if avg < 0:
        return "negative_on_known"
    return "flat"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BATCH-15 summary aggregator (B3)")
    ap.add_argument("--root", required=True,
                    help="корень batch15-артефактов (с b1/, b2/, b3/)")
    ap.add_argument("--out", required=True, help="каталог вывода (создаётся)")
    args = ap.parse_args(argv)

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    missing: list[str] = []
    for cid in CANDIDATE_IDS:
        group_num = {"B0": "b1", "B1": "b2"}.get(cid[:2], "b2")
        if cid in ("B01", "B02", "B03", "B04", "B05"):
            group_num = "b1"
        elif cid in ("B06", "B07", "B08", "B09", "B10"):
            group_num = "b2"
        else:
            group_num = "b3"
        group_letter = GROUP_DIRS[group_num]
        for ticker in TICKERS:
            for cost_id in COST_IDS:
                summary, src = _load_cell(root, cid, ticker, cost_id)
                row = _cell_row(summary, group_letter, cid, ticker, cost_id)
                if summary is None:
                    missing.append(f"{cid}|{ticker}|{cost_id}: {src}")
                rows.append(row)

    results_csv = out / "results.csv"
    with results_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULTS_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in RESULTS_FIELDS})

    by_cand: dict[str, list[dict]] = {}
    for r in rows:
        by_cand.setdefault(r["candidate_id"], []).append(r)

    lines = [
        "# BATCH-15 — итоговая таблица (development screening)",
        "",
        f"Сгенерировано: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Все 15 кандидатов смотрели на ОДНОМ development-периоде "
        "(warmup 2026-06-08, eval 2026-06-11..2026-10-08, immutable A10 CSV). "
        "Последующая проверка лидеров требует НОВОГО нетронутого периода; "
        "текущая выборка НЕ является holdout.",
        "",
        "net/entry_base — для единичного номинала одного инструмента, "
        "НЕ доходность портфеля. Стоимостные сценарии (5/2 и 10/5 bps) — "
        "ПАРНЫЕ наблюдения, не независимые. INCOMPLETE без числового net: "
        "исход неизвестен. Незакрытые ячейки отмечены явно.",
        "",
        "## Сводка по кандидатам (15 строк; детали по 120 ячейкам — results.csv)",
        "",
        "| Кандид | Группа | FILLED (c5/c10) | INCOMPLETE | net_mean (avg) | Вердикт |",
        "|---|---|---|---|---|---|",
    ]
    for cid in CANDIDATE_IDS:
        cand_rows = by_cand.get(cid, [])
        group = cand_rows[0].get("group", "?") if cand_rows else "?"
        filled_c5 = sum(r.get("FILLED") or 0 for r in cand_rows
                        if r.get("cost_scenario") == "c5_s2")
        filled_c10 = sum(r.get("FILLED") or 0 for r in cand_rows
                         if r.get("cost_scenario") == "c10_s5")
        incomplete = sum(r.get("INCOMPLETE") or 0 for r in cand_rows)
        means = [r.get("net_mean_per_entry") for r in cand_rows
                 if isinstance(r.get("net_mean_per_entry"), (int, float))]
        avg_mean = f"{sum(means) / len(means):+.6f}" if means else "—"
        verdict = _verdict(cand_rows)
        lines.append(
            f"| {cid} | {group} | {filled_c5}/{filled_c10} | {incomplete} | "
            f"{avg_mean} | {verdict} |")

    if missing:
        lines += ["", f"## Отсутствующие ячейки: {len(missing)}", ""]
        for m in missing[:20]:
            lines.append(f"- {m}")
        if len(missing) > 20:
            lines.append(f"- …(+{len(missing) - 20})")

    errors = [r for r in rows if r.get("overall_status") in ("ERROR", "MISSING")]
    lines += ["", f"## Ячейки со статусом ERROR/MISSING: {len(errors)}", ""]
    for r in errors:
        lines.append(f"- {r.get('candidate_id')}|{r.get('ticker')}|"
                     f"{r.get('cost_scenario')}: "
                     f"{r.get('overall_status')} {r.get('cell_error', '')[:120]}")

    results_md = out / "RESULTS.md"
    results_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"BATCH-15 summary: rows={len(rows)} missing={len(missing)} "
          f"errors={len(errors)}")
    print(f"  {results_csv}")
    print(f"  {results_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
