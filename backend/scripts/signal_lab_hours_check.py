#!/usr/bin/env python
"""Часы × лейблы × стороны по замороженным ячейкам: что ещё говорят сигналы.

По обоим периодам (sep/aug): hold-24 net = sign(side)*future_return - 0.14%.
Агрегация по часу МСК, лейблу V2 и стороне. Ищем, есть ли концентрация эджа
в конкретных часах/лейблах, которая объясняет результаты лаборатории.
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
    args = ap.parse_args()
    eng = engine_sync()
    with open(OUT / "frozen_selected.csv", encoding="utf-8") as f:
        cells = list(csv.DictReader(f))
    engines = sorted({r["family"] for r in cells})
    keep = {(r["family"], r["tf"], r["side"], r["regime"]) for r in cells}
    ph = ",".join(f":e{i}" for i in range(len(engines)))
    sd = "CASE WHEN e.side='BUY' THEN 1 ELSE -1 END"
    data = {}
    for spec in args.runs:
        rid, label = spec.split(":", 1)
        params = {"r": rid, "nl": NULL_LABEL, "cost": COST}
        params.update({f"e{i}": e for i, e in enumerate(engines)})
        sql = f"""
          SELECT e.strategy_id, e.tf, e.side,
                 COALESCE(r.measurements->>'label', :nl) label,
                 EXTRACT(HOUR FROM (e.start_ts::timestamptz + interval '3 hours'))::int AS hr,
                 count(*) n,
                 avg(({sd}) * o.future_return - :cost) avg_net,
                 avg(((({sd}) * o.future_return - :cost) > 0)::int) win
          FROM lab_signal_event_runs e
          JOIN lab_market_outcomes o
            ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
          LEFT JOIN lab_regime_observations r
            ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
          WHERE e.run_id = :r AND e.strategy_id IN ({ph})
          GROUP BY 1,2,3,4,5
        """
        for row in eng.connect().execute(text(sql), params):
            k = (row[0], row[1], row[2], row[3])
            if k not in keep:
                continue
            data[(label,) + k + (int(row[4]),)] = {
                "n": int(row[5]), "avg": float(row[6] or 0), "win": float(row[7] or 0)}

    # 1) Профиль по часам для всех SELL NEUTRAL-ячеек (оба периода вместе)
    hours = defaultdict(lambda: [0, 0.0, 0])  # hour -> [n, sum, wins]
    for (period, e, tf, side, lab, hour), v in data.items():
        if side != "SELL":
            continue
        if lab != "NEUTRAL":
            continue
        agg = hours[hour]
        agg[0] += v["n"]
        agg[1] += v["avg"] * v["n"]
        agg[2] += v["win"] * v["n"]
    print("=== SELL NEUTRAL (все семьи): net-hold по часам МСК (оба периода):")
    for h in sorted(hours):
        n, s, w = hours[h]
        if n >= 50:
            print(f"  {h:02d}:00 n={n:>5} avg={s/n*100:+.3f}% win={w/n*100:3.0f}%")
    print()
    # 2) По лейблам и сторонам — сводка (оба периода)
    lab_side = defaultdict(lambda: [0, 0.0, 0])
    for (period, e, tf, side, lab, hour), v in data.items():
        a = lab_side[(lab, side)]
        a[0] += v["n"]
        a[1] += v["avg"] * v["n"]
        a[2] += v["win"] * v["n"]
    print("=== лейбл × сторона (все выбранные ячейки, оба периода):")
    for k in sorted(lab_side, key=lambda x: -lab_side[x][1] / max(lab_side[x][0], 1)):
        n, s, w = lab_side[k]
        if n >= 100:
            print(f"  {k[0]:16s} {k[1]:4s} n={n:>6} avg={s/n*100:+.3f}% win={w/n*100:3.0f}%")
    print()
    # 3) Профиль rsi_reversal 5m SELL NEUTRAL по часам отдельно по периодам
    for period in sorted({k[0] for k in data}):
        print(f"=== rsi_reversal 5m SELL NEUTRAL · {period}, по часам:")
        rows = []
        for (p, e, tf, side, lab, hour), v in data.items():
            if p == period and e == "rsi_reversal" and tf == "5min" and side == "SELL" and lab == "NEUTRAL":
                rows.append((hour, v))
        rows.sort()
        for hour, v in rows:
            if v["n"] >= 10:
                print(f"  {hour:02d}:00 n={v['n']:>4} avg={v['avg']*100:+.3f}% win={v['win']*100:3.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
