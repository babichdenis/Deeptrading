#!/usr/bin/env python
"""Hold-to-horizon net-проверка замороженных ячеек: direction-only, без стопов.

net = sign(side) * future_return - round_trip_cost (2*commission + 2*slippage).
Отвечает: «если не ставить внутридневной SL/TP, а держать 12/24 бара ТФ,
сколько остаётся после издержек». Считаем по обоим периодам.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="run_id:label ...")
    ap.add_argument("--horizons", default="12,24")
    ap.add_argument("--cells", default=str(OUT / "frozen_selected.csv"))
    args = ap.parse_args()
    eng = engine_sync()
    with open(args.cells, encoding="utf-8") as f:
        cells = list(csv.DictReader(f))
    engines = sorted({r["family"] for r in cells})
    keep = {(r["family"], r["tf"], r["side"], r["regime"]) for r in cells}
    ph = ",".join(f":e{i}" for i in range(len(engines)))
    horizons = [int(x) for x in args.horizons.split(",")]
    hph = ",".join(str(h) for h in horizons)
    cost = 2 * 0.0005 + 2 * 0.0002  # комиссия + слиппедж с обеих сторон
    all_rows = {}
    for spec in args.runs:
        rid, label = spec.split(":", 1)
        params = {"r": rid, "nl": NULL_LABEL}
        params.update({f"e{i}": e for i, e in enumerate(engines)})
        sd = "CASE WHEN e.side='BUY' THEN 1 ELSE -1 END"
        sql = f"""
          SELECT e.strategy_id, e.tf, e.side,
                 COALESCE(r.measurements->>'label', :nl) label, o.horizon_bars,
                 count(*) n,
                 avg(({sd}) * o.future_return - :cost) avg_net,
                 percentile_disc(0.5) WITHIN GROUP (
                   ORDER BY ({sd}) * o.future_return - :cost) med_net,
                 avg(((({sd}) * o.future_return - :cost) > 0)::int) win,
                 sum(({sd}) * o.future_return - :cost) sum_net
          FROM lab_signal_event_runs e
          JOIN lab_market_outcomes o
            ON o.signal_id = e.start_signal_id AND o.horizon_bars IN ({hph})
          LEFT JOIN lab_regime_observations r
            ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
          WHERE e.run_id = :r AND e.strategy_id IN ({ph})
          GROUP BY 1,2,3,4,5
        """
        for row in eng.connect().execute(text(sql), {**params, "cost": cost}):
            k = (row[0], row[1], row[2], row[3], row[4], label)
            if (row[0], row[1], row[2], row[3]) not in keep:
                continue
            all_rows[k] = {"n": int(row[5]), "avg": float(row[6] or 0),
                           "med": float(row[7] or 0), "win": float(row[8] or 0),
                           "sum": float(row[9] or 0)}
    # свести периоды
    merged = {}
    for (e, tf, side, lab, h, period), v in all_rows.items():
        merged.setdefault((e, tf, side, lab, h), {})[period] = v
    out = []
    for k, per in sorted(merged.items()):
        row = {"engine": k[0], "tf": k[1], "side": k[2], "regime": k[3], "h": k[4]}
        for p, v in per.items():
            row[f"{p}_n"] = v["n"]
            row[f"{p}_avg"] = round(v["avg"] * 100, 4)
            row[f"{p}_med"] = round(v["med"] * 100, 4)
            row[f"{p}_win"] = round(v["win"], 3)
            row[f"{p}_sum"] = round(v["sum"] * 100, 2)
        out.append(row)
    periods = sorted({s.split(":", 1)[1] for s in args.runs})
    cols = ["engine", "tf", "side", "regime", "h"]
    for p in periods:
        cols += [f"{p}_n", f"{p}_avg", f"{p}_med", f"{p}_win", f"{p}_sum"]
    with open(OUT / "hold_pnl_selected.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(f"[csv] hold_pnl_selected.csv: {len(out)} строк; cost={cost*100:.2f}% round-trip",
          flush=True)
    both = [r for r in out
            if all(int(r.get(f"{p}_n", 0)) >= 30 for p in periods)
            and all(float(r.get(f"{p}_avg", -1)) > 0 for p in periods)]
    both.sort(key=lambda r: sum(float(r.get(f"{p}_avg", 0)) for p in periods), reverse=True)
    print(f"=== ячеек прибыльных (avg net>0) во ВСЕХ периодах, n>=30: {len(both)}", flush=True)
    for r in both[:25]:
        seg = " | ".join(
            f"{p}: n={r[f'{p}_n']} avg={r[f'{p}_avg']:+.3f}% med={r[f'{p}_med']:+.3f}% "
            f"win={r[f'{p}_win']*100:.0f}%" for p in periods)
        print(f"  {r['engine']:20s} {r['tf']:5s} {r['side']:4s} {r['regime']:15s} h={r['h']:>2} {seg}",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
