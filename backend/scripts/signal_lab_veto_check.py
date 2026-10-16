#!/usr/bin/env python
"""Проверка соседей-вето: прав ли сам B, когда он обнуляет якорь A.

Для каждого (A=якорная SELL-ячейка, B=сосед ±5 мин):
  A_with  — forward return якоря, когда B рядом (gross, 24 бара)
  B_own   — forward return САМОГО B в те же моменты (gross, его сторона)
B_own > 0 — B «прав»: рынок идёт против якоря (просто не бери A).
B_own < 0 — B сам ошибается: соседство маркирует шумную точку (никто не работает).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.lab.data import engine_sync  # noqa: E402

OUT = BACKEND / "reports" / "signal_lab" / "events"
NULL_LABEL = "(no_regime)"
VETO = ["donchian_breakout", "neutral_ensemble", "two_timeframes_hub",
        "price_channel_hub", "trend_up", "range_ensemble"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="run_id:label ...")
    ap.add_argument("--tol-min", type=int, default=5)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()
    eng = engine_sync()
    with open(OUT / "frozen_selected.csv", encoding="utf-8") as f:
        cells = [r for r in csv.DictReader(f) if r["side"] == "SELL"]
    keep = {(r["family"], r["tf"], r["side"], r["regime"]) for r in cells}
    aeng = sorted({r["family"] for r in cells})
    vph = ",".join(f":v{i}" for i in range(len(VETO)))
    out_rows = []
    for spec in args.runs:
        rid, period = spec.split(":", 1)
        with eng.connect() as c:
            c.execute(text("DROP TABLE IF EXISTS _ev"))
            c.execute(text("DROP TABLE IF EXISTS _an"))
            c.execute(text("""
                CREATE TEMP TABLE _ev AS
                SELECT e.figi, e.strategy_id, e.side,
                       floor(extract(epoch FROM e.start_ts::timestamptz) / 60)::bigint AS b,
                       CASE WHEN e.side='BUY' THEN o.future_return ELSE -o.future_return END AS gross
                FROM lab_signal_event_runs e
                JOIN lab_market_outcomes o
                  ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
                WHERE e.run_id = :r
            """), {"r": rid})
            c.execute(text("CREATE INDEX ON _ev (figi, b)"))
            sql_an = """
                CREATE TEMP TABLE _an AS
                SELECT e.figi, e.strategy_id, e.tf, e.side,
                       COALESCE(r.measurements->>'label', :nl) AS label,
                       floor(extract(epoch FROM e.start_ts::timestamptz) / 60)::bigint AS b,
                       CASE WHEN e.side='BUY' THEN o.future_return ELSE -o.future_return END AS gross
                FROM lab_signal_event_runs e
                JOIN lab_market_outcomes o
                  ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
                LEFT JOIN lab_regime_observations r
                  ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
                WHERE e.run_id = :r AND e.side = 'SELL'
                  AND e.strategy_id IN ({ph})
            """.format(ph=",".join(f":a{i}" for i in range(len(aeng))))
            c.execute(text(sql_an), {"r": rid, "nl": NULL_LABEL,
                                     **{f"a{i}": e for i, e in enumerate(aeng)}})
            c.execute(text("CREATE INDEX ON _an (figi, b)"))
            sql = f"""
                SELECT a.strategy_id, a.tf, a.side, a.label, e2.strategy_id AS other,
                       count(*) AS n, sum(a.gross) AS sa, sum(e2.gross) AS sb
                FROM _an a
                JOIN generate_series(-:tol, :tol) AS d ON true
                JOIN _ev e2 ON e2.figi = a.figi AND e2.b = a.b + d
                           AND e2.strategy_id <> a.strategy_id
                           AND e2.strategy_id IN ({vph})
                GROUP BY 1,2,3,4,5
            """
            params = {"tol": args.tol_min}
            params.update({f"v{i}": e for i, e in enumerate(VETO)})
            for row in c.execute(text(sql), params):
                key = (row[0], row[1], row[2], row[3])
                if key not in keep:
                    continue
                n = int(row[5])
                if n < args.min_n:
                    continue
                out_rows.append([period, key[0], key[1], key[2], key[3], row[4],
                                 n, round(float(row[6] or 0) / n * 100, 4),
                                 round(float(row[7] or 0) / n * 100, 4)])
            c.execute(text("DROP TABLE IF EXISTS _ev"))
            c.execute(text("DROP TABLE IF EXISTS _an"))
        print(f"  {period}: собрано", flush=True)

    with open(OUT / "veto_check.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["period", "anchor", "tf", "side", "regime", "neighbor",
                    "n", "A_with_pct", "B_own_pct"])
        w.writerows(out_rows)
    print(f"[csv] veto_check.csv: {len(out_rows)} строк", flush=True)
    by = {}
    for r in out_rows:
        by.setdefault((r[1], r[2], r[3], r[4], r[5]), {})[r[0]] = r
    print("=== A с соседом-вето vs собственный ход B (sep / aug):", flush=True)
    items = []
    for k, p in by.items():
        if "sep" in p and "aug" in p:
            items.append((k, p["sep"], p["aug"]))
    items.sort(key=lambda x: x[1][7] + x[2][7])
    for k, s, a in items[:20]:
        print(f"  {k[0]:18s} {k[1]:5s} {k[3]:15s} + {k[4]:18s} "
              f"A_with {s[7]:+.3f}/{a[7]:+.3f}%  B_own {s[8]:+.3f}/{a[8]:+.3f}%  "
              f"(n={s[6]}/{a[6]})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
