#!/usr/bin/env python
"""Значения функций в точках входа vs вне них — отдельно LONG/SHORT и по режимам.

Для каждой функции F и каждого числового ключа её features:
  entry-группа  = значения F в её сигналах в ±tol мин от точек входа
                  (anchors = frozen_selected, отдельно BUY/SELL и по лейблу режима)
  baseline      = значения F по всем её сигналам периода
Сравниваем квартили: где функция «живёт» на входе против обычного состояния.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.lab.data import engine_sync  # noqa: E402

OUT = BACKEND / "reports" / "signal_lab" / "events"
NULL_LABEL = "(no_regime)"
NUM_RE = r"^-?[0-9]+(\.[0-9]+)?$"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="run_id:label ...")
    ap.add_argument("--tol-min", type=int, default=3)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()
    eng = engine_sync()
    with open(OUT / "frozen_selected.csv", encoding="utf-8") as f:
        cells = list(csv.DictReader(f))
    akeys = [("|".join((r["family"], r["tf"], r["side"], r["regime"])), r["side"], r["regime"])
             for r in cells]
    out = []
    for spec in args.runs:
        rid, period = spec.split(":", 1)
        with eng.connect() as c:
            # numeric-ключи по функциям
            keys_by_engine: dict[str, list[str]] = {}
            for row in c.execute(text("""
                SELECT strategy_id, k FROM (
                  SELECT DISTINCT strategy_id, jsonb_object_keys(features::jsonb) k
                  FROM lab_signal_events
                  WHERE run_id = :r AND features::text NOT IN ('{}','null','')
                ) t ORDER BY 1, 2
            """), {"r": rid}):
                keys_by_engine.setdefault(row[0], []).append(row[1])
            # пул raw-событий с бакетами
            c.execute(text("DROP TABLE IF EXISTS _raw"))
            c.execute(text("""
                CREATE TEMP TABLE _raw AS
                SELECT strategy_id, side, features::jsonb AS f,
                       floor(extract(epoch FROM bar_ts::timestamptz) / 60)::bigint AS b,
                       figi
                FROM lab_signal_events WHERE run_id = :r
                  AND features::text NOT IN ('{}','null','')
            """), {"r": rid})
            c.execute(text("CREATE INDEX ON _raw (strategy_id)"))
            # якоря: только нужные ячейки, с бакетом
            c.execute(text("DROP TABLE IF EXISTS _anc"))
            sql_anc = """
                CREATE TEMP TABLE _anc AS
                SELECT e.strategy_id, e.tf, e.side,
                       COALESCE(r.measurements->>'label', :nl) AS label,
                       floor(extract(epoch FROM e.start_ts::timestamptz) / 60)::bigint AS b,
                       e.figi
                FROM lab_signal_event_runs e
                LEFT JOIN lab_regime_observations r
                  ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
                WHERE e.run_id = :r
                  AND (e.strategy_id || '|' || e.tf || '|' || e.side || '|'
                       || COALESCE(r.measurements->>'label', :nl)) IN ({ph})
            """.format(ph=",".join(f":c{i}" for i in range(len(akeys))))
            c.execute(text(sql_anc), {"r": rid, "nl": NULL_LABEL,
                                      **{f"c{i}": k[0] for i, k in enumerate(akeys)}})
            c.execute(text("CREATE INDEX ON _anc (side, b)"))
            for fn, keys in keys_by_engine.items():
                for key in keys:
                    # baseline: все сигналы функции
                    b = c.execute(text(f"""
                        SELECT count(*),
                               percentile_disc(0.25) WITHIN GROUP (
                                 ORDER BY (f->>'{key}')::float8),
                               percentile_disc(0.5) WITHIN GROUP (
                                 ORDER BY (f->>'{key}')::float8),
                               percentile_disc(0.75) WITHIN GROUP (
                                 ORDER BY (f->>'{key}')::float8)
                        FROM _raw
                        WHERE strategy_id = :fn AND (f->>'{key}') ~ :re
                    """), {"fn": fn, "re": NUM_RE}).fetchone()
                    if not b or not b[0]:
                        continue
                    # entry: сигналы функции рядом с якорями, по стороне и режиму
                    rows = c.execute(text(f"""
                        SELECT a.side, a.label, count(*),
                               percentile_disc(0.25) WITHIN GROUP (
                                 ORDER BY (r.f->>'{key}')::float8),
                               percentile_disc(0.5) WITHIN GROUP (
                                 ORDER BY (r.f->>'{key}')::float8),
                               percentile_disc(0.75) WITHIN GROUP (
                                 ORDER BY (r.f->>'{key}')::float8)
                        FROM _anc a
                        JOIN generate_series(-:tol, :tol) AS d ON true
                        JOIN _raw r ON r.strategy_id = :fn AND r.figi = a.figi
                                   AND r.b = a.b + d
                        WHERE (r.f->>'{key}') ~ :re
                        GROUP BY 1,2
                    """), {"fn": fn, "tol": args.tol_min, "re": NUM_RE})
                    for row in rows:
                        if int(row[2]) < args.min_n:
                            continue
                        out.append([period, fn, key, row[0], row[1], int(row[2]),
                                    round(float(row[3]), 6), round(float(row[4]), 6),
                                    round(float(row[5]), 6),
                                    int(b[0]), round(float(b[1]), 6),
                                    round(float(b[2]), 6), round(float(b[3]), 6)])
            c.execute(text("DROP TABLE IF EXISTS _raw"))
            c.execute(text("DROP TABLE IF EXISTS _anc"))
        print(f"  {period}: готово", flush=True)
    with open(OUT / "value_zones.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["period", "function", "key", "side", "regime", "n_entry",
                    "e_p25", "e_med", "e_p75", "n_base", "b_p25", "b_med", "b_p75"])
        w.writerows(out)
    print(f"[csv] value_zones.csv: {len(out)} строк", flush=True)
    # сдвиг медианы entry vs base
    sh = [r for r in out if r[9] >= 100 and r[5] >= args.min_n]
    sh.sort(key=lambda r: -abs(r[7] - r[11]))
    print("=== ТОП сдвигов значения (entry vs baseline, |Δmed|):", flush=True)
    for r in sh[:25]:
        print(f"  {r[1]:<22s} {r[2]:<14s} {r[3]:4s} {r[4]:<15s} "
              f"entry_med={r[7]:+.4f} [{r[6]:+.4f}..{r[8]:+.4f}] "
              f"base_med={r[11]:+.4f} n={r[5]}/{r[9]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
