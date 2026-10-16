#!/usr/bin/env python
"""Функция × режим в точках входа: кто и в каком режиме попадал рядом.

Точки входа (label points) = события замороженных ячеек (frozen_selected).
Для каждой функции (движка) считаем, в каких режимах (V2-лейбл точки входа)
она оказывалась в пределах допуска:
  ±1..3 минутных свечей  -> tol = 1/2/3 мин
  ±1..3 пятиминутных     -> tol = 5/10/15 мин
Выдаём матрицу функция × режим (число точек, где функция была рядом) и CSV.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.lab.data import engine_sync  # noqa: E402

OUT = BACKEND / "reports" / "signal_lab" / "events"
NULL_LABEL = "(no_regime)"
TOLS = [1, 3, 5, 15]
LABELS = ["RANGE", "NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", NULL_LABEL]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="run_id:label ...")
    args = ap.parse_args()
    eng = engine_sync()
    with open(OUT / "frozen_selected.csv", encoding="utf-8") as f:
        cells = list(csv.DictReader(f))
    ckeys = ["|".join((r["family"], r["tf"], r["side"], r["regime"])) for r in cells]
    cph = ",".join(f":c{i}" for i in range(len(ckeys)))
    rows = []
    for spec in args.runs:
        rid, period = spec.split(":", 1)
        with eng.connect() as c:
            c.execute(text("DROP TABLE IF EXISTS _ev"))
            c.execute(text("DROP TABLE IF EXISTS _an"))
            c.execute(text("""
                CREATE TEMP TABLE _ev AS
                SELECT figi, strategy_id,
                       floor(extract(epoch FROM start_ts::timestamptz) / 60)::bigint AS b
                FROM lab_signal_event_runs WHERE run_id = :r
            """), {"r": rid})
            c.execute(text("CREATE INDEX ON _ev (figi, b)"))
            sql_an = """
                CREATE TEMP TABLE _an AS
                SELECT e.figi, e.strategy_id, e.tf, e.side,
                       COALESCE(r.measurements->>'label', :nl) AS label,
                       floor(extract(epoch FROM e.start_ts::timestamptz) / 60)::bigint AS b
                FROM lab_signal_event_runs e
                LEFT JOIN lab_regime_observations r
                  ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
                WHERE e.run_id = :r
                  AND (e.strategy_id || '|' || e.tf || '|' || e.side || '|'
                       || COALESCE(r.measurements->>'label', :nl)) IN ({cph})
            """.format(cph=cph)
            c.execute(text(sql_an), {"r": rid, "nl": NULL_LABEL,
                                     **{f"c{i}": k for i, k in enumerate(ckeys)}})
            c.execute(text("CREATE INDEX ON _an (figi, b)"))
            for tol in TOLS:
                sql = """
                    SELECT e2.strategy_id, a.label, count(*) AS n
                    FROM _an a
                    JOIN generate_series(-:tol, :tol) AS d ON true
                    JOIN _ev e2 ON e2.figi = a.figi AND e2.b = a.b + d
                               AND e2.strategy_id <> a.strategy_id
                    GROUP BY 1,2
                """
                for row in c.execute(text(sql), {"tol": tol}):
                    key = (row[0], row[1])
                    # ключ ячейки (family,tf,side,label) — только чтобы отфильтровать
                    rows.append([period, tol, row[0], row[1], int(row[2])])
            c.execute(text("DROP TABLE IF EXISTS _ev"))
            c.execute(text("DROP TABLE IF EXISTS _an"))
        print(f"  {period}: собрано", flush=True)

    # агрегируем: суммируем по двум периодам
    agg = defaultdict(int)
    for period, tol, fn, lab, n in rows:
        if lab not in LABELS:
            continue
        agg[(tol, fn, lab)] += n
    funcs = sorted({k[1] for k in agg})
    with open(OUT / "function_regime_hits.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tol_min", "function", "regime", "hits", "share_pct"])
        for tol in TOLS:
            tot_tol = sum(v for (t, _, _), v in agg.items() if t == tol)
            for fn in funcs:
                for lab in LABELS:
                    n = agg.get((tol, fn, lab), 0)
                    if n:
                        w.writerow([tol, fn, lab, n,
                                    round(n / max(tot_tol, 1) * 100, 4)])
    print(f"[csv] function_regime_hits.csv: {len(agg)} строк", flush=True)

    for tol in (3, 15):
        print(f"=== функция × режим, допуск ±{tol} мин (сумма sep+aug, попаданий):")
        hdr = "  {:<22s}".format("function") + "".join(
            f"{l[:8]:>10s}" for l in LABELS)
        print(hdr)
        for fn in funcs:
            vals = [agg.get((tol, fn, l), 0) for l in LABELS]
            if sum(vals) < 50:
                continue
            print("  {:<22s}".format(fn) + "".join(f"{v:>10d}" for v in vals))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
