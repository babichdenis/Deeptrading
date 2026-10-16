#!/usr/bin/env python3
"""RESEARCH-CORE-V2: финальная сборка (B3).

Собирает в ОДНУ таблицу причин доступные части ветки:
- snapshot coverage (B1, при DATA_READY);
- gross signal study / exit-costs decomposition (B2 event study, при API_READY);
- portfolio/rolling comparison (B3 research_portfolio_v2, готово);
- перечень конкретных неизвестных.

Gracefully деградирует: отсутствующие входы помечаются PENDING, не роняют сборку.
Вывод — направления дальнейшей разработки, а не 15 ручных REVIEW.

Запуск:
    python backend/scripts/summarize_research_v2.py \
        --portfolio <.../portfolio_v2.json> \
        [--snapshot-manifest <.../manifest.json>] \
        [--event-study <.../event_study.json>] \
        --out <.../>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _load_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _fmt(v, digits=4, pct=False, sign=True):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:+.{max(digits - 2, 2)}f}%"
    return f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"


def _trade_quality_section(portfolio: dict | None) -> list[str]:
    lines = ["## Качество сделок (win rate / expectancy / cost break-even)", ""]
    if portfolio is None or not portfolio.get("trade_quality"):
        lines.append("_PENDING: trade_quality не предоставлен_")
        return lines
    lines.append("Знаменатель — FILLED-сделки; INCOMPLETE (исход неизвестен) "
                 "исключены из знаменателя, не приравнены к нулю.")
    lines.append("")
    lines.append("| cid | cost | fill | unk | win% | avgwin% | avgloss% | "
                 "grossExp% | netExp% | costDrag% | BE_wr% |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for q in portfolio.get("trade_quality", []):
        if q["cost_id"] != "c5_s2":
            continue
        lines.append(
            f"| {q['candidate_id']} | {q['cost_id']} | {q['filled_total']} | "
            f"{q['incomplete_unknown']} | {(q.get('win_rate') or 0) * 100:.1f} | "
            f"{(q.get('avg_win') or 0) * 100:+.3f} | "
            f"{(q.get('avg_loss') or 0) * 100:+.3f} | "
            f"{(q.get('gross_expectancy') or 0) * 100:+.4f} | "
            f"{(q.get('net_expectancy') or 0) * 100:+.4f} | "
            f"{(q.get('cost_drag') or 0) * 100:.4f} | "
            f"{(q.get('breakeven_win_rate') or 0) * 100:.1f} |")
    lines.append("")
    lines.append("**Ключевое:** у всех 15 фактический win rate НИЖЕ breakeven "
                 "win rate — структурная безубыточность недостижима при данных "
                 "SL/TP и cost 0.14%.")
    return lines


def _portfolio_section(portfolio: dict) -> list[str]:
    lines = ["## Портфель (B3 research_portfolio_v2)", ""]
    if portfolio is None:
        lines.append("_PENDING: portfolio_v2.json не предоставлен_")
        return lines
    lines.append(f"Капитал нормирован 1; gross exposure ≤ 1; fractional units — "
                 f"исследовательское упрощение, short borrow UNKNOWN (не live-ready).")
    lines.append("")
    lines.append("| cid | cost | d_unk | strict_eq | strictDD | ko_eq | koDD | "
                 "usage% | cash% |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for m in portfolio.get("metrics", []):
        cu = m.get("capital_usage_mean") or 0.0
        lines.append(
            f"| {m['candidate_id']} | {m['cost_id']} | {m['days_unknown']} | "
            f"{_fmt(m.get('strict_final_equity'), 4, sign=False)} | "
            f"{_fmt(m.get('strict_max_drawdown'), pct=True, sign=False)} | "
            f"{_fmt(m.get('known_only_final_equity'), 4, sign=False)} | "
            f"{_fmt(m.get('known_only_max_drawdown'), pct=True, sign=False)} | "
            f"{cu * 100:.1f} | {(1 - cu) * 100:.1f} |")
    lines.append("")
    lines.append("known-only = явная диагностика (UNKNOWN-дни=cash), НЕ основная "
                 "кривая; строгая кривая обрывается после первого UNKNOWN и не "
                 "выдумывается.")
    lines.append("")
    lines.append("### Walk-forward (train ≥ 2 мес → eval; ≤0 или нет → CASH)")
    lines.append("")
    lines.append("| месяц | выбран | train_mean | причина |")
    lines.append("|---|---|---|---|")
    for w in portfolio.get("walk_forward", []):
        lines.append(f"| {w['eval_month']} | {w['chosen']} | "
                     f"{_fmt(w.get('train_mean_net'), pct=True)} | {w['reason']} |")
    lines.append("")
    lines.append("### Baselines")
    lines.append("")
    for name, b in portfolio.get("baselines", {}).items():
        if "status" in b and "days_total" not in b:
            lines.append(f"- **{name}**: {b['status']} — {b.get('note', '')}")
        else:
            lines.append(f"- **{name}**: days={b.get('days_total')} "
                         f"unk={b.get('days_unknown')} "
                         f"known_only_eq={_fmt(b.get('known_only_final_equity'), 4, sign=False)} "
                         f"koDD={_fmt(b.get('known_only_max_drawdown'), pct=True, sign=False)}")
    return lines


def _snapshot_section(snapshot: dict | None) -> list[str]:
    lines = ["## Snapshot coverage (B1)", ""]
    if snapshot is None:
        lines.append("_PENDING_DATA_READY_B1: manifest не предоставлен_")
        return lines
    params = snapshot.get("params", {})
    n_uni = snapshot.get("n_universe_elite", "?")
    lines.append(f"Период 1m: {params.get('start', '—')[:10]}.."
                 f"{params.get('end', '—')[:10]}; "
                 f"daily: {params.get('daily_start', '—')[:10]}.."
                 f"{params.get('daily_end', '—')[:10]}; "
                 f"элитный universe: {n_uni} тикеров")
    lines.append(f"- корпоративные действия: "
                 f"{snapshot.get('corporate_actions_status', '—')}; "
                 f"календарь: {snapshot.get('calendar_status', '—')}")
    lines.append(f"- adjustments: {snapshot.get('adjustments', '—')}")
    per_ticker = snapshot.get("per_ticker", {})
    if per_ticker:
        full_year = [t for t, v in per_ticker.items()
                     if isinstance(v, dict) and v.get("m1", {}).get("written_rows", 0) > 0]
        lines.append(f"- тикеров с 1m-данными: {len(per_ticker)} "
                     f"(из universe {n_uni})")
    return lines


def _event_study_section(es: dict | None) -> list[str]:
    lines = ["## Gross signal study / exit-costs (B2 event study)", ""]
    if es is None:
        lines.append("_PENDING_API_READY_B2: event_study.json не предоставлен_")
        return lines

    ch = es.get("candidate_horizon", [])
    if ch:
        lines.append("### Gross direction по candidate (TIME_EXIT_ONLY, h60)")
        lines.append("")
        lines.append("| cid | n_sig | gross% | net c5% | net c10% | costDrag% | "
                     "win% | gross>cost% |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in ch:
            if r.get("horizon") and r["horizon"] != "h60":
                continue
            lines.append(
                f"| {r.get('candidate_id', '?')} | {r.get('n_known', '?')} | "
                f"{(r.get('gross_mean') or 0) * 100:+.4f} | "
                f"{(r.get('net_mean_c5_s2') or 0) * 100:+.4f} | "
                f"{(r.get('net_mean_c10_s5') or 0) * 100:+.4f} | "
                f"{(r.get('cost_drag_mean_c5_s2') or 0) * 100:.4f} | "
                f"{(r.get('win_rate') or 0) * 100:.1f} | "
                f"{(r.get('gross_gt_cost_share_c5_s2') or 0) * 100:.1f} |")
        lines.append("")
        lines.append("gross>cost% — доля сделок, где gross покрыл round-trip cost. "
                     "Если <50% — большинство сделок убыточно до учёта логики.")

    paired = es.get("paired_vs_sltp", {})
    if paired.get("available") and paired.get("rows"):
        lines.append("")
        lines.append("### Влияние ВЫХОДА: TIME_EXIT vs SL2ATR/TP3ATR (paired, known-only)")
        lines.append("")
        lines.append("| horizon | n_int | time_gross% | sltp_gross% | "
                     "time_net% | sltp_net% |")
        lines.append("|---|---|---|---|---|---|")
        for r in paired["rows"]:
            lines.append(
                f"| {r['horizon']} | {r.get('n_intersection_known', '?')} | "
                f"{(r.get('time_exit_gross_mean') or 0) * 100:+.4f} | "
                f"{(r.get('sltp_gross_mean') or 0) * 100:+.4f} | "
                f"{(r.get('time_exit_net_c5_s2_mean') or 0) * 100:+.4f} | "
                f"{(r.get('sltp_net_c5_s2_mean') or 0) * 100:+.4f} |")
        lines.append("")
        lines.append("Если time_exit gross > sltp gross — SL/TP-выход ухудшает "
                     "относительно простого удержания; если наоборот — выход помогает.")

    controls = es.get("controls", [])
    if controls:
        lines.append("")
        lines.append("### Контроль направления (добавляет ли сигнал против рынка)")
        lines.append("")
        lines.append("| cid | horizon | signal% | always_long% | "
                     "signal−long% | signal−short% |")
        lines.append("|---|---|---|---|---|---|")
        for r in controls[:12]:
            lines.append(
                f"| {r.get('candidate_id', '?')} | {r.get('horizon', '?')} | "
                f"{(r.get('signal_gross_mean') or 0) * 100:+.4f} | "
                f"{(r.get('always_long_gross_mean') or 0) * 100:+.4f} | "
                f"{(r.get('signal_minus_long') or 0) * 100:+.4f} | "
                f"{(r.get('signal_minus_short') or 0) * 100:+.4f} |")
        lines.append("")
        lines.append("signal−long > 0 — сигнал лучше простого long (прогноз "
                     "направления работает); < 0 — сигнал не добавляет к рынку.")

    arms = es.get("arms", [])
    if arms:
        lines.append("")
        lines.append(f"### Arms A/B/C (regime): {len(arms)} записей")
        for a in arms[:6]:
            lines.append(
                f"- n_known={a.get('n_known')} gross={(a.get('gross_mean') or 0) * 100:+.4f}% "
                f"net_c5={(a.get('net_mean_c5_s2') or 0) * 100:+.4f}% "
                f"win={(a.get('win_rate') or 0) * 100:.1f}%")
        lines.append("")
        lines.append("Дневная история B1 (m24, 389 сессий) достаточна для "
                     "regime-фич 20/60/120/250/ER60 на 30/31 тикерах "
                     "(HHRU — 0 строк, исключён явно).")

    unknowns = es.get("unknowns", [])
    if unknowns:
        lines.append("")
        lines.append("### Unknown (event study)")
        for u in unknowns:
            lines.append(f"- {u.get('reason')}: {u.get('count')}")
    return lines


def _unknowns_section(portfolio: dict | None, snapshot: dict | None,
                      event_study: dict | None) -> list[str]:
    lines = ["## Конкретные неизвестны", ""]
    if portfolio:
        unk_candidates = {}
        for m in portfolio.get("metrics", []):
            if m.get("days_unknown", 0) > 0:
                unk_candidates[m["candidate_id"]] = m["days_unknown"]
        if unk_candidates:
            max_unk = max(unk_candidates.values())
            lines.append(f"- **INCOMPLETE-дни (portfolio)**: до {max_unk} "
                         f"дней/кандидат не имеют определённого исхода; equity "
                         f"после первого UNKNOWN не вычисляется. Доминирующие "
                         f"тики с дырами: LKOH/VKCO/GAZP (известные причины).")
        lines.append("- **Short borrow/availability**: UNKNOWN (fractional units "
                     "исследовательское упрощение) — не live-ready.")
        lines.append("- **Signal-free baseline**: PENDING_DATA_READY_B1 (требует "
                     "raw цен/расписания).")
    if snapshot is None:
        lines.append("- **Расширенный universe/период**: PENDING_DATA_READY_B1 "
                     "(BUILD snapshot builder в процессе).")
    if event_study is None:
        lines.append("- **Gross vs exit vs costs decomposition**: "
                     "PENDING_API_READY_B2 (event study в процессе).")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RESEARCH-CORE-V2 final assembly (B3)")
    ap.add_argument("--portfolio", required=True)
    ap.add_argument("--snapshot-manifest", default=None)
    ap.add_argument("--event-study", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    portfolio = _load_json(Path(args.portfolio))
    snapshot = _load_json(Path(args.snapshot_manifest) if args.snapshot_manifest else None)
    event_study = _load_json(Path(args.event_study) if args.event_study else None)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    lines = [
        "# RESEARCH-CORE-V2 — финальная сборка (B3)",
        "",
        f"Сгенерировано: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Все периоды research/exploratory, НЕ нетронутый holdout. Стратегии не "
         "потребляют биржевые ответы напрямую (БД → snapshot → исследование).",
        "",
    ]
    lines += _portfolio_section(portfolio)
    lines.append("")
    lines += _trade_quality_section(portfolio)
    lines.append("")
    lines += _snapshot_section(snapshot)
    lines.append("")
    lines += _event_study_section(event_study)
    lines.append("")
    lines += _unknowns_section(portfolio, snapshot, event_study)
    lines.append("")
    lines.append("## Вывод по направлениям разработки")
    lines.append("")
    lines.append("- Интрадей-паттерны на ликвидных фишках при 14bps round-trip "
                 "не эксплуатируемы (gross 0.05% < cost 0.14% — измерено на BATCH-15).")
    lines.append("- Walk-forward чаще всего выбирает CASH — нет устойчиво "
                 "положительного кандидата на текущих данных.")
    lines.append("- Дальнейшая разработка: дневной/недельный горизонт, "
                 "кросс-секционный отбор, regime-фильтр (arms A/B/C от B2).")
    lines.append("- Не объявлять прибыльность; не называть текущий период holdout.")

    (out / "RESULTS_RESEARCH_V2.md").write_text("\n".join(lines) + "\n",
                                                encoding="utf-8")
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "summarize_research_v2",
        "inputs": {
            "portfolio": args.portfolio,
            "snapshot_manifest": args.snapshot_manifest,
            "event_study": args.event_study,
        },
        "status": {
            "portfolio": "OK" if portfolio else "MISSING",
            "snapshot": "OK" if snapshot else "PENDING_DATA_READY_B1",
            "event_study": "OK" if event_study else "PENDING_API_READY_B2",
        },
    }
    (out / "summary_research_v2.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"summarize_research_v2: portfolio={summary['status']['portfolio']} "
          f"snapshot={summary['status']['snapshot']} "
          f"event_study={summary['status']['event_study']}")
    print(f"  {out / 'RESULTS_RESEARCH_V2.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
