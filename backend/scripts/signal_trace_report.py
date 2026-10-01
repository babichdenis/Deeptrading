#!/usr/bin/env python3
"""Signal Trace — аналитический отчёт по прогону (фаза 4-лайт).

Читает signal_trace_events + signal_trace_outcomes и печатает срезы:
  воронка и причины отказов; outcomes по горизонтам; TP/SL-статистика
  по тикерам, стороне, RSI-бакетам, часу (МСК) и режиму рынка.

Ничего не пишет. Запуск (на .7):
  .venv/bin/python scripts/signal_trace_report.py --run-key "fullt 0901-05 ..." --horizon 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402


def _engine():
    return create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)


RAW_CTE = """
WITH raw AS (
    SELECT DISTINCT ON (signal_id) signal_id, ticker, side, ts_bar,
           (context->'features'->>'rsi')::float AS rsi,
           context->'context'->'regime'->>'state' AS regime
    FROM signal_trace_events
    WHERE run_id = :r AND stage = 'RAW' AND signal_id IS NOT NULL
    ORDER BY signal_id, seq
), outc AS (
    SELECT signal_id, label, horizon_bars FROM signal_trace_outcomes
)
"""


def _pivot(eng, rid: str, h: int, group_expr: str, order_expr: str, title: str) -> None:
    q = RAW_CTE + f"""
    SELECT {group_expr} AS grp,
           count(*) AS n,
           count(*) FILTER (WHERE o.label = 'TP_FIRST') AS tp,
           count(*) FILTER (WHERE o.label = 'SL_FIRST') AS sl,
           count(*) FILTER (WHERE o.label = 'TIMEOUT') AS to_
    FROM raw r JOIN outc o ON o.signal_id = r.signal_id AND o.horizon_bars = :h
    GROUP BY 1 ORDER BY {order_expr}"""
    with eng.connect() as c:
        rows = c.execute(text(q), {"r": rid, "h": h}).all()
    print(f"\n== {title} (горизонт {h}м):")
    print(f"  {'группа':<22} {'N':>4} {'TP':>4} {'SL':>4} {'TIMEOUT':>7} {'TP/SL':>7}")
    for g, n, tp, sl, to in rows:
        ratio = f"{tp / sl:.2f}" if sl else ("inf" if tp else "—")
        print(f"  {str(g):<22} {n:>4} {tp:>4} {sl:>4} {to:>7} {ratio:>7}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-key", required=True)
    ap.add_argument("--horizon", type=int, default=60)
    a = ap.parse_args()
    eng = _engine()
    with eng.connect() as c:
        run = c.execute(text(
            "SELECT run_id, stats FROM signal_trace_runs WHERE run_key=:k ORDER BY started_at DESC LIMIT 1"
        ), {"k": a.run_key}).mappings().first()
        if not run:
            print(f"run не найден: {a.run_key!r}")
            return 1
        rid = run["run_id"]
        print(f"RUN: {a.run_key}")
        print("stats:", run["stats"])
        q = c.execute(text(
            "SELECT stage, status, count(*) FROM signal_trace_events WHERE run_id=:r GROUP BY 1,2 ORDER BY 3 DESC"
        ), {"r": rid}).all()
        print("воронка:", q)
        q = c.execute(text(
            "SELECT reason_code, count(*) FROM signal_trace_events WHERE run_id=:r AND status='REJECTED' "
            "GROUP BY 1 ORDER BY 2 DESC"
        ), {"r": rid}).all()
        print("отказы по причинам (FILL/REJECTED):", q)
        q = c.execute(text(
            "SELECT horizon_bars, count(*) FILTER (WHERE label='TP_FIRST') tp, "
            "count(*) FILTER (WHERE label='SL_FIRST') sl, count(*) FILTER (WHERE label='TIMEOUT') to_ "
            "FROM signal_trace_outcomes o JOIN signal_trace_events e "
            "ON e.signal_id=o.signal_id AND e.run_id=:r GROUP BY 1 ORDER BY 1"
        ), {"r": rid}).all()
        print("outcomes по горизонтам (h, TP, SL, TIMEOUT):", q)
        trades = c.execute(text(
            "SELECT exit_reason, count(*) FROM sandbox_trades WHERE test_name=:k GROUP BY 1"
        ), {"k": a.run_key}).all()
        print("сделки:", trades)

    _pivot(eng, rid, a.horizon, "r.ticker", "n DESC", "По тикерам")
    _pivot(eng, rid, a.horizon, "r.side", "grp", "По стороне")
    _pivot(eng, rid, a.horizon, "to_char(r.ts_bar + interval '3 hours', 'HH24:00')", "grp", "По часу МСК")
    _pivot(eng, rid, a.horizon,
           "CASE WHEN r.rsi < 30 THEN '<30' WHEN r.rsi < 40 THEN '30-40' WHEN r.rsi < 50 THEN '40-50' "
           "WHEN r.rsi < 60 THEN '50-60' WHEN r.rsi < 70 THEN '60-70' ELSE '>=70' END",
           "grp", "По RSI сигнала")
    _pivot(eng, rid, a.horizon, "coalesce(r.regime, '—')", "n DESC", "По режиму")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
