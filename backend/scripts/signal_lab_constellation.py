#!/usr/bin/env python
"""Созвездие сигналов вокруг валидированных входов (±N минут, все ТФ).

Реализация через минутные бакеты (temp-таблица + индекс) — range-джойн по
интервалу на 2.35M событий не укладывается в statement_timeout.

Для каждого «якорного» SELL-события ищем события ДРУГИХ движков в том же figi
в бакетах ±tol минут (любой ТФ) и сравниваем hold-24 net якоря с соседом vs без.
Разбивка sep/aug — ищем соседей, усиливающих вход стабильно в оба периода.
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
COST = 0.0014


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
    print(f"якорей (SELL-ячейки): {len(cells)}; движков-якорей: {len(aeng)}", flush=True)

    stats: dict = {}
    totals: dict = {}
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
            _sql_an = """
                CREATE TEMP TABLE _an AS
                SELECT e.start_signal_id, e.figi, e.strategy_id, e.tf, e.side,
                       COALESCE(r.measurements->>'label', :nl) AS label,
                       floor(extract(epoch FROM e.start_ts::timestamptz) / 60)::bigint AS b,
                       (CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) * o.future_return - :cost AS net
                FROM lab_signal_event_runs e
                JOIN lab_market_outcomes o
                  ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
                LEFT JOIN lab_regime_observations r
                  ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
                WHERE e.run_id = :r AND e.side = 'SELL'
                  AND e.strategy_id IN ({ph})
            """.format(ph=",".join(f":a{i}" for i in range(len(aeng))))
            c.execute(text(_sql_an), {"r": rid, "nl": NULL_LABEL, "cost": COST,
                                      **{f"a{i}": e for i, e in enumerate(aeng)}})
            c.execute(text("CREATE INDEX ON _an (figi, b)"))
            sql = """
                SELECT a.strategy_id, a.tf, a.side, a.label, e2.strategy_id AS other,
                       count(*) AS n, sum(a.net) AS s
                FROM _an a
                JOIN generate_series(-:tol, :tol) AS d ON true
                JOIN _ev e2 ON e2.figi = a.figi AND e2.b = a.b + d
                           AND e2.strategy_id <> a.strategy_id
                GROUP BY 1,2,3,4,5
            """
            for row in c.execute(text(sql), {"tol": args.tol_min}):
                key = (row[0], row[1], row[2], row[3])
                if key not in keep:
                    continue
                stats[(period, key, row[4])] = {"n": int(row[5]), "s": float(row[6] or 0)}
            for row in c.execute(text("""
                SELECT strategy_id, tf, side, label, count(*), sum(net)
                FROM _an GROUP BY 1,2,3,4
            """)):
                key = (row[0], row[1], row[2], row[3])
                if key in keep:
                    totals[(period, key)] = (int(row[4]), float(row[5] or 0))
            c.execute(text("DROP TABLE IF EXISTS _ev"))
            c.execute(text("DROP TABLE IF EXISTS _an"))
        print(f"  {period}: собрано", flush=True)

    rows = []
    for (period, key, other), v in stats.items():
        if v["n"] < args.min_n:
            continue
        tn, ts = totals.get((period, key), (0, 0.0))
        n_without = tn - v["n"]
        if n_without < args.min_n:
            continue
        avg_with = v["s"] / v["n"] * 100
        avg_without = (ts - v["s"]) / n_without * 100
        rows.append([period, key[0], key[1], key[2], key[3], other,
                     v["n"], round(avg_with, 4), round(avg_without, 4),
                     round(avg_with - avg_without, 4)])
    with open(OUT / "constellation.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["period", "anchor", "tf", "side", "regime", "other_engine",
                    "n_with", "avg_with", "avg_without", "delta"])
        w.writerows(rows)
    print(f"[csv] constellation.csv: {len(rows)} строк", flush=True)

    by = defaultdict(dict)
    for r in rows:
        by[(r[1], r[2], r[3], r[4], r[5])][r[0]] = r
    stable = []
    for k, per in by.items():
        if "sep" in per and "aug" in per:
            d1, d2 = per["sep"][9], per["aug"][9]
            if (d1 > 0.05 and d2 > 0.05) or (d1 < -0.05 and d2 < -0.05):
                stable.append((k, per["sep"], per["aug"]))
    stable.sort(key=lambda x: -(x[1][9] + x[2][9]))
    print(f"=== СТАБИЛЬНЫЕ соседи (±{args.tol_min} мин, оба периода, |Δ|>0.05):", flush=True)
    for k, s, a in stable[:18]:
        print(f"  {k[0]:18s} {k[1]:5s} {k[3]:15s} + {k[4]:18s} "
              f"sep Δ={s[9]:+.3f}% (n={s[6]}, with={s[7]:+.3f} vs {s[8]:+.3f}) | "
              f"aug Δ={a[9]:+.3f}% (n={a[6]}, with={a[7]:+.3f} vs {a[8]:+.3f})", flush=True)
    anti = stable[-8:][::-1]
    print("=== СТАБИЛЬНО ВРЕДНЫЕ соседи:", flush=True)
    for k, s, a in anti:
        print(f"  {k[0]:18s} {k[1]:5s} {k[3]:15s} + {k[4]:18s} "
              f"sep Δ={s[9]:+.3f}% | aug Δ={a[9]:+.3f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
