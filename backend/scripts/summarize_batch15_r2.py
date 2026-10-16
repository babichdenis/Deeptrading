#!/usr/bin/env python3
"""BATCH-15 summary-r2 (B3). Исправленная отчётность поверх сохранённых артефактов.

Дефект исходного runner-а (B2, исправляется в его зоне): insufficient_context
классифицировался по буквальному reason=="insufficient_context", тогда как evaluate
выдаёт type=insufficient_context с конкретным reason (incomplete_window/
atr_warmup_insufficient/...). Итог — ложные нули insufficient_context и завышенный SKIP.

Этот скрипт НЕ переписывает старые артефакты и НЕ пересчитывает торговлю: он читает
сохранённые trades.csv + skipped.csv и восстанавливает классификацию через ЯВНОЕ
отображение reason -> event_type. Неизвестная причина -> счётчик UNKNOWN (не выдумывается).

Два описательных показателя по каждому candidate×cost (costs РАЗДЕЛЬНО, пары не складываются):
- equal-ticker mean net/entry_base — среднее средних по тикерам (невзвешенное);
- pooled-trade mean net/entry_base — среднее по ВСЕМ сделкам (взвешено числом сделок).
Плюс число сделок/unknown и концентрация по тикеру (макс. доля FILLED).
net/entry_base — единичный номинал одного инструмента, НЕ доходность портфеля.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

GROUP_DIRS = ("b1", "b2", "b3")
CANDIDATE_IDS = (
    "B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09", "B10",
    "B11", "B12", "B13", "B14", "B15",
)
TICKERS = ("GAZP", "LKOH", "SFIN", "VKCO")
COST_IDS = ("c5_s2", "c10_s5")

REASON_TO_TYPE = {
    "no_signal": "skip",
    "weekend_excluded": "skip",
    "missing_entry": "skip",
    "entry_minute_missing": "skip",
    "no_signal_theta": "skip",
    "no_signal_continuation": "skip",
    "no_gap": "skip",
    "incomplete_window": "insufficient_context",
    "incomplete_hour": "insufficient_context",
    "atr_warmup_insufficient": "insufficient_context",
    "atr_invalid": "insufficient_context",
    "vwap_volume_zero": "insufficient_context",
}

R2_FIELDS = (
    "candidate_id", "group", "cost_scenario",
    "FILLED", "INCOMPLETE", "SKIP", "insufficient_context", "UNKNOWN",
    "signals_total", "unknown_reasons",
    "pooled_net_mean_per_entry", "equal_ticker_net_mean_per_entry",
    "max_ticker_share", "max_ticker", "per_ticker_filled",
    "positive_tickers", "negative_tickers",
    "coverage_status", "spec_hash",
)


def _find_cell(root: Path, cid: str, ticker: str, cost_id: str) -> Path | None:
    for gd in GROUP_DIRS:
        for run_dir in sorted(root.glob(f"{gd}/[0-9]*")):
            cell = run_dir / f"{cid}__{ticker}__{cost_id}"
            if cell.exists():
                return cell
    return None


def _read_trades(cell: Path) -> list[dict]:
    f = cell / "trades.csv"
    if not f.exists():
        return []
    with f.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_skips(cell: Path) -> list[dict]:
    f = cell / "skipped.csv"
    if not f.exists():
        return []
    with f.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _classify_skips(skips: list[dict]) -> tuple[int, int, int, list[str]]:
    """-> (skip_count, insufficient_count, unknown_count, unknown_reasons)."""
    skip_n = 0
    insuf_n = 0
    unknown_n = 0
    unknown_reasons: list[str] = []
    for s in skips:
        reason = s.get("reason", "")
        etype = REASON_TO_TYPE.get(reason)
        if etype == "skip":
            skip_n += 1
        elif etype == "insufficient_context":
            insuf_n += 1
        else:
            unknown_n += 1
            if reason not in unknown_reasons:
                unknown_reasons.append(reason)
    return skip_n, insuf_n, unknown_n, unknown_reasons


def _cell_metrics(cell: Path, cid: str, ticker: str, cost_id: str,
                  group: str) -> dict:
    trades = _read_trades(cell)
    skips = _read_skips(cell)
    filled = [t for t in trades if t.get("status") == "FILLED"]
    incomplete = [t for t in trades if t.get("status") == "INCOMPLETE"]
    skip_n, insuf_n, unknown_n, unknown_reasons = _classify_skips(skips)

    ratios_by_ticker: dict[str, list[float]] = {}
    all_ratios: list[float] = []
    for t in filled:
        try:
            r = float(t["net"]) / float(t["entry_base"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        ratios_by_ticker.setdefault(ticker, []).append(r)
        all_ratios.append(r)

    per_ticker_means = {tk: statistics.mean(v)
                        for tk, v in ratios_by_ticker.items() if v}
    equal_ticker = (statistics.mean(per_ticker_means.values())
                    if per_ticker_means else None)
    pooled = statistics.mean(all_ratios) if all_ratios else None

    n_filled = len(filled)
    max_ticker = ""
    max_share = None
    if n_filled:
        best_tk, best_n = max(ratios_by_ticker.items(),
                              key=lambda kv: len(kv[1]))
        max_ticker = best_tk
        max_share = len(best_n) / n_filled

    positive = [tk for tk, m in per_ticker_means.items() if m > 0]
    negative = [tk for tk, m in per_ticker_means.items() if m < 0]

    spec_hash = ""
    pre = cell / "preflight.json"
    if pre.exists():
        try:
            spec_hash = json.loads(pre.read_text(encoding="utf-8")).get(
                "spec_hash", "")
        except (json.JSONDecodeError, OSError):
            pass

    coverage = "OK"
    if unknown_n:
        coverage = f"UNKNOWN_REASONS({unknown_n})"
    if not (cell / "trades.csv").exists():
        coverage = "MISSING_TRADES"

    return {
        "candidate_id": cid, "group": group, "cost_scenario": cost_id,
        "FILLED": n_filled, "INCOMPLETE": len(incomplete),
        "SKIP": skip_n, "insufficient_context": insuf_n,
        "UNKNOWN": unknown_n,
        "signals_total": n_filled + len(incomplete),
        "unknown_reasons": ";".join(unknown_reasons),
        "pooled_net_mean_per_entry": pooled,
        "equal_ticker_net_mean_per_entry": equal_ticker,
        "max_ticker_share": max_share, "max_ticker": max_ticker,
        "per_ticker_filled": json.dumps(
            {tk: len(v) for tk, v in ratios_by_ticker.items()}, sort_keys=True),
        "positive_tickers": ",".join(sorted(positive)),
        "negative_tickers": ",".join(sorted(negative)),
        "coverage_status": coverage, "spec_hash": spec_hash,
    }


def _fmt(v, digits=6, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:+.{max(digits - 2, 2)}f}%"
    return f"{v:.{digits}f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BATCH-15 summary-r2 aggregator (B3)")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    missing: list[str] = []
    for cid in CANDIDATE_IDS:
        group = ("b1" if cid in ("B01", "B02", "B03", "B04", "B05")
                 else "b2" if cid in ("B06", "B07", "B08", "B09", "B10")
                 else "b3")
        for cost_id in COST_IDS:
            per_ticker_cells = []
            for ticker in TICKERS:
                cell = _find_cell(root, cid, ticker, cost_id)
                if cell is None:
                    missing.append(f"{cid}|{ticker}|{cost_id}")
                    continue
                per_ticker_cells.append(
                    _cell_metrics(cell, cid, ticker, cost_id, group))
            rows.append(_rollup(cid, group, cost_id, per_ticker_cells))

    results_csv = out / "results_r2.csv"
    with results_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=R2_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in R2_FIELDS})

    md = _render_md(rows, missing)
    (out / "RESULTS_R2.md").write_text(md, encoding="utf-8")

    print(f"BATCH-15 summary-r2: rows={len(rows)} missing={len(missing)}")
    print(f"  {results_csv}")
    print(f"  {out / 'RESULTS_R2.md'}")
    return 0


def _rollup(cid: str, group: str, cost_id: str, cells: list[dict]) -> dict:
    """Свёртка 4 тикеров в одну строку candidate×cost (costs раздельно)."""
    if not cells:
        return {"candidate_id": cid, "group": group, "cost_scenario": cost_id,
                "coverage_status": "MISSING_ALL", "FILLED": 0, "INCOMPLETE": 0,
                "SKIP": 0, "insufficient_context": 0, "UNKNOWN": 0,
                "signals_total": 0, "unknown_reasons": "",
                "pooled_net_mean_per_entry": None,
                "equal_ticker_net_mean_per_entry": None,
                "max_ticker_share": None, "max_ticker": "",
                "per_ticker_filled": "{}", "positive_tickers": "",
                "negative_tickers": "", "spec_hash": ""}
    filled = sum(c["FILLED"] for c in cells)
    incomplete = sum(c["INCOMPLETE"] for c in cells)
    skip_n = sum(c["SKIP"] for c in cells)
    insuf = sum(c["insufficient_context"] for c in cells)
    unknown = sum(c["UNKNOWN"] for c in cells)
    unknown_reasons: list[str] = []
    per_ticker: dict[str, int] = {}
    positive: list[str] = []
    negative: list[str] = []
    pooled_all: list[float] = []
    equal_means: list[float] = []
    max_ticker = ""
    max_share = None
    spec_hash = ""

    for c in cells:
        pt = json.loads(c["per_ticker_filled"] or "{}")
        for tk, n in pt.items():
            per_ticker[tk] = per_ticker.get(tk, 0) + n
        if c["positive_tickers"]:
            positive += c["positive_tickers"].split(",")
        if c["negative_tickers"]:
            negative += c["negative_tickers"].split(",")
        if c["unknown_reasons"]:
            unknown_reasons += c["unknown_reasons"].split(";")
        if not spec_hash:
            spec_hash = c["spec_hash"]

    pooled = None
    if filled:
        weighted = 0.0
        for c in cells:
            if c["pooled_net_mean_per_entry"] is not None:
                weighted += c["pooled_net_mean_per_entry"] * c["FILLED"]
        pooled = weighted / filled

    equal_means: list[float] = []
    for c in cells:
        if c["equal_ticker_net_mean_per_entry"] is not None:
            equal_means.append(c["equal_ticker_net_mean_per_entry"])

    if per_ticker:
        max_ticker, max_n = max(per_ticker.items(), key=lambda kv: kv[1])
        max_share = max_n / filled if filled else None

    equal_ticker = statistics.mean(equal_means) if equal_means else None

    unknown_reasons = sorted(set(unknown_reasons))
    coverage = "OK"
    if unknown:
        coverage = f"UNKNOWN_REASONS({unknown})"
    if not cells:
        coverage = "MISSING_ALL"
    elif len(cells) < len(TICKERS):
        coverage = f"PARTIAL_COVERAGE({len(cells)}/{len(TICKERS)})"

    return {
        "candidate_id": cid, "group": group, "cost_scenario": cost_id,
        "FILLED": filled, "INCOMPLETE": incomplete, "SKIP": skip_n,
        "insufficient_context": insuf, "UNKNOWN": unknown,
        "signals_total": filled + incomplete,
        "unknown_reasons": ";".join(unknown_reasons),
        "pooled_net_mean_per_entry": pooled,
        "equal_ticker_net_mean_per_entry": equal_ticker,
        "max_ticker_share": max_share, "max_ticker": max_ticker,
        "per_ticker_filled": json.dumps(per_ticker, sort_keys=True),
        "positive_tickers": ",".join(sorted(set(positive))),
        "negative_tickers": ",".join(sorted(set(negative))),
        "coverage_status": coverage, "spec_hash": spec_hash,
    }


def _render_md(rows: list[dict], missing: list[str]) -> str:
    by_cand: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_cand.setdefault(r["candidate_id"], {})[r["cost_scenario"]] = r

    lines = [
        "# BATCH-15 — summary-r2 (исправленная отчётность)",
        "",
        f"Сгенерировано: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Что исправлено",
        "",
        "Классификация insufficient_context восстановлена по ЯВНОМУ отображению "
        "reason -> event_type (исходный runner считал только буквальный "
        "reason=='insufficient_context', давая ложные нули и завышенный SKIP). "
        "Неизвестные причины -> счётчик UNKNOWN (не выдумываются). "
        "Артефакты не переписывались, торговля не пересчитывалась.",
        "",
        "## Два показателя (costs РАЗДЕЛЬНО, пары не складываются)",
        "",
        "- **equal-ticker mean** net/entry_base — среднее средних по тикерам "
        "(невзвешенное; чувствительно к тикерам с малым числом сделок);",
        "- **pooled-trade mean** net/entry_base — среднее по ВСЕМ сделкам "
        "(взвешено числом сделок).",
        "",
        "net/entry_base — единичный номинал одного инструмента, НЕ доходность "
        "портфеля. Сценарии costs (5/2 и 10/5 bps) — ПАРНЫЕ наблюдения, не "
        "независимые. INCOMPLETE без числового net: исход неизвестен. "
        "Проверка лидеров требует нового нетронутого периода; текущая выборка "
        "НЕ holdout.",
        "",
        "## Итоговая таблица (15 кандидатов × 2 costs = 30 строк)",
        "",
        "| Кандид | cost | FILLED | INCOMPLETE | SKIP | insuf | UNKNOWN | "
        "pooled mean | equal-ticker mean | концентрация | +тики/−тики |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cid in CANDIDATE_IDS:
        for cost_id in COST_IDS:
            r = by_cand.get(cid, {}).get(cost_id)
            if r is None:
                lines.append(f"| {cid} | {cost_id} | — | — | — | — | — | — | — | — | — |")
                continue
            conc = "—"
            if r["max_ticker_share"] is not None:
                conc = f"{r['max_ticker']} {r['max_ticker_share'] * 100:.0f}%"
            lines.append(
                f"| {cid} | {cost_id} | {r['FILLED']} | {r['INCOMPLETE']} | "
                f"{r['SKIP']} | {r['insufficient_context']} | {r['UNKNOWN']} | "
                f"{_fmt(r['pooled_net_mean_per_entry'], pct=True)} | "
                f"{_fmt(r['equal_ticker_net_mean_per_entry'], pct=True)} | "
                f"{conc} | {r['positive_tickers'] or '—'}/"
                f"{r['negative_tickers'] or '—'} |")

    lines += ["", "## Покрытие", ""]
    unknown_cells = [r for r in rows if r["UNKNOWN"]]
    lines.append(f"Ячеек с UNKNOWN-причинами: {len(unknown_cells)}")
    if unknown_cells:
        for r in unknown_cells:
            lines.append(f"- {r['candidate_id']}|{r['cost_scenario']}: "
                         f"{r['unknown_reasons']}")
    lines.append(f"Отсутствующие ячейки: {len(missing)}")
    for m in missing[:20]:
        lines.append(f"- {m}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
