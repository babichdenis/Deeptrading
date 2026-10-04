#!/usr/bin/env python
"""Валидация замороженных September-ячеек на августовском прогоне (back-check).

Читает frozen_cells.csv (все 471 ячейки; selected — подмножество), считает те же
метрики на validation-прогоне (events × outcomes × regime) и сравнивает:
  REPLICATED — n>=50, med>0, P(+0)>50%
  PARTIAL    — n>=50, med>0, но P(+0)<=50%
  FAILED     — n>=50, med<=0
  THIN       — n<50
  NO_DATA    — нет событий
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
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--frozen", default=str(OUT / "frozen_cells.csv"))
    args = ap.parse_args()
    eng = engine_sync()
    rid = args.run_id

    with open(args.frozen, encoding="utf-8") as f:
        frozen = list(csv.DictReader(f))
    engines = sorted({r["family"] for r in frozen})
    ph = ",".join(f":e{i}" for i in range(len(engines)))
    params = {"r": rid, "nl": NULL_LABEL}
    params.update({f"e{i}": e for i, e in enumerate(engines)})
    sd = "o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END"
    sql = f"""
      SELECT e.strategy_id, e.tf, e.side,
             COALESCE(r.measurements->>'label', :nl) label,
             count(*) n, count(DISTINCT e.figi) tickers,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY {sd}) med,
             avg(({sd} > 0)::int) p0,
             avg(({sd} > 0.5)::int) p05
      FROM lab_signal_event_runs e
      JOIN lab_market_outcomes o
        ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
      LEFT JOIN lab_regime_observations r
        ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
      WHERE e.run_id = :r AND e.strategy_id IN ({ph})
      GROUP BY 1,2,3,4
    """
    val = {}
    for r in eng.connect().execute(text(sql), params):
        val[(r[0], r[1], r[2], r[3])] = {
            "n": int(r[4]), "tickers": int(r[5]),
            "med": float(r[6]) if r[6] is not None else None,
            "p0": float(r[7] or 0), "p05": float(r[8] or 0)}

    rows = []
    for c in frozen:
        v = val.get((c["family"], c["tf"], c["side"], c["regime"]))
        n_v = v["n"] if v else 0
        med_v = v["med"] if v else None
        if not v or n_v == 0:
            verdict = "NO_DATA"
        elif n_v < 50:
            verdict = "THIN"
        elif med_v is not None and med_v > 0 and v["p0"] > 0.5:
            verdict = "REPLICATED"
        elif med_v is not None and med_v > 0:
            verdict = "PARTIAL"
        else:
            verdict = "FAILED"
        rows.append({
            "selected": c["selected"], "family": c["family"], "tf": c["tf"],
            "side": c["side"], "regime": c["regime"],
            "disc_n": c["n"], "disc_med": c["discovery_median"],
            "disc_p0": c["discovery_p_positive"],
            "val_n": n_v,
            "val_med": round(med_v, 4) if med_v is not None else None,
            "val_p0": round(v["p0"], 4) if v else None,
            "val_p05": round(v["p05"], 4) if v else None,
            "val_tickers": v["tickers"] if v else 0,
            "verdict": verdict,
        })
    with open(OUT / "validation_frozen.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    sel = [r for r in rows if r["selected"] == "True"]
    from collections import Counter
    cnt_all = Counter(r["verdict"] for r in rows)
    cnt_sel = Counter(r["verdict"] for r in sel)
    print(f"всего ячеек: {len(rows)}; вердикты: {dict(cnt_all)}", flush=True)
    print(f"selected: {len(sel)}; вердикты: {dict(cnt_sel)}", flush=True)
    ok = [r for r in sel if r["verdict"] in ("REPLICATED", "PARTIAL")]
    ok.sort(key=lambda r: -int(r["val_n"]))
    print("=== selected, подтверждённые на августе (REPLICATED/PARTIAL):", flush=True)
    for r in ok:
        print(f"  {r['family']:24s} {r['tf']:6s} {r['side']:4s} {r['regime']:16s} "
              f"disc(med={r['disc_med']}, n={r['disc_n']}) -> "
              f"aug(med={r['val_med']:+.2f}, n={r['val_n']}, P0={r['val_p0']*100:.0f}%, "
              f"tick={r['val_tickers']})", flush=True)
    bad = [r for r in sel if r["verdict"] in ("FAILED", "NO_DATA", "THIN")]
    print(f"=== selected, НЕ подтверждённые: {len(bad)} (FAILED/NO_DATA/THIN)", flush=True)
    for r in bad[:12]:
        print(f"  {r['family']:24s} {r['tf']:6s} {r['side']:4s} {r['regime']:16s} "
              f"n={r['val_n']} med={r['val_med']} [{r['verdict']}]", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
