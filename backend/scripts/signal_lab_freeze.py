#!/usr/bin/env python
"""Заморозка September discovery перед OOS (по правилу, не по «красивым» ячейкам).

Правило отбора ПРЕ-РЕГИСТРИРУЕТСЯ здесь и больше не меняется до OOS:
  - n >= 300 событий
  - med >= +0.30 ATR (signed)
  - P(>0) >= 52%
  - покрытие >= 10 тикеров
  - обе половины сентября: med_H1 > 0 и med_H2 > 0

Пишет: frozen_cells.csv (все 471), frozen_selected.csv, FREEZE.json (хэши
каталога семей, regime-определения и кода).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.lab.data import engine_sync  # noqa: E402

OUT = BACKEND / "reports" / "signal_lab" / "events"
HALF = "2026-09-16"
RULES = {
    "min_events": 300,
    "min_median_atr": 0.30,
    "min_p_positive": 0.52,
    "min_tickers": 10,
    "require_both_halves_positive": True,
}
NULL_LABEL = "(no_regime)"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:16]


def code_hash() -> str:
    h = hashlib.sha256()
    for rel in ("app/lab/regime.py", "app/lab/events.py",
                "scripts/signal_lab_families.py", "scripts/signal_lab_regime_events.py",
                "app/services/regime_v2/provider.py", "app/services/regime_v2/classifier.py",
                "app/services/regime_v2/hysteresis.py", "app/services/regime_v2/measurements.py",
                "app/services/regime_v2/observation.py"):
        f = BACKEND / rel
        if f.exists():
            h.update(rel.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    eng = engine_sync()
    rid = args.run_id
    sd = "o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END"
    sql = f"""
      SELECT e.strategy_id, e.tf, e.side,
             COALESCE(r.measurements->>'label', :nl) label,
             count(*) n, count(DISTINCT e.figi) tickers,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY {sd}) med,
             avg(({sd} > 0)::int) p0,
             avg(({sd} > 0.5)::int) p05,
             avg(({sd} > 1.0)::int) p1,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY {sd})
               FILTER (WHERE e.start_ts < :half) med_h1,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY {sd})
               FILTER (WHERE e.start_ts >= :half) med_h2,
             count(*) FILTER (WHERE e.start_ts < :half) n_h1,
             count(*) FILTER (WHERE e.start_ts >= :half) n_h2
      FROM lab_signal_event_runs e
      JOIN lab_market_outcomes o
        ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
      LEFT JOIN lab_regime_observations r
        ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
      WHERE e.run_id = :r
      GROUP BY 1,2,3,4
    """
    rows = eng.connect().execute(text(sql), {"r": rid, "nl": NULL_LABEL, "half": HALF}).fetchall()

    fam = {}
    fpath = OUT / "family_catalog.csv"
    with open(fpath, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["relationship"] == "CANONICAL":
                fam[r["canonical_engine"]] = r["family_id"]

    reg = eng.connect().execute(text(
        "SELECT DISTINCT version, params_hash FROM lab_regime_observations")).fetchall()

    cells = []
    for r in rows:
        eng_name, tf, side, label = r[0], r[1], r[2], r[3]
        reasons = []
        if label == NULL_LABEL:
            reasons.append("no_regime")
        if int(r[4]) < RULES["min_events"]:
            reasons.append(f"n<{RULES['min_events']}")
        if r[6] is None or float(r[6]) < RULES["min_median_atr"]:
            reasons.append("med<0.3")
        if float(r[7] or 0) < RULES["min_p_positive"]:
            reasons.append("P(+0)<52%")
        if int(r[5]) < RULES["min_tickers"]:
            reasons.append(f"tickers<{RULES['min_tickers']}")
        if not (r[10] is not None and r[11] is not None
                and float(r[10]) > 0 and float(r[11]) > 0):
            reasons.append("halves_not_both_positive")
        selected = not reasons
        cells.append({
            "family_id": fam.get(eng_name, ""), "family": eng_name, "tf": tf,
            "side": side, "regime": label, "n": int(r[4]),
            "min_events": RULES["min_events"],
            "discovery_median": round(float(r[6]), 4) if r[6] is not None else None,
            "discovery_p_positive": round(float(r[7] or 0), 4),
            "discovery_p_05atr": round(float(r[8] or 0), 4),
            "discovery_p_1atr": round(float(r[9] or 0), 4),
            "tickers": int(r[5]),
            "med_h1": round(float(r[10]), 4) if r[10] is not None else None,
            "med_h2": round(float(r[11]), 4) if r[11] is not None else None,
            "n_h1": int(r[12] or 0), "n_h2": int(r[13] or 0),
            "selected": selected,
            "selection_reason": "OK" if selected else ";".join(reasons),
        })
    cells.sort(key=lambda c: (-int(c["selected"]),
                              -(c["discovery_median"] or -99)))

    cols = list(cells[0].keys())
    with open(OUT / "frozen_cells.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(cells)
    sel = [c for c in cells if c["selected"]]
    with open(OUT / "frozen_selected.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sel)

    manifest = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "run_id": rid,
        "discovery_period": "2026-09-01..2026-09-30",
        "oos_period": "2026-10-01..<not_started>",
        "rules": RULES,
        "family_catalog_sha256": sha256_file(fpath),
        "pairs_all_sha256": sha256_file(OUT / "pairs_all.csv")
        if (OUT / "pairs_all.csv").exists() else None,
        "regime_version": sorted({r[0] for r in reg}),
        "regime_params_hash": sorted({r[1] for r in reg}),
        "lab_code_sha256": code_hash(),
        "cells_total": len(cells),
        "cells_selected": len(sel),
        "frozen_cells_sha256": sha256_file(OUT / "frozen_cells.csv"),
    }
    with open(OUT / "FREEZE.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps({"cells": len(cells), "selected": len(sel),
                      "regime_hash": manifest["regime_params_hash"],
                      "code_hash": manifest["lab_code_sha256"]},
                     ensure_ascii=False), flush=True)
    print("=== ОТОБРАННЫЕ ЯЧЕЙКИ:", flush=True)
    for c in sel:
        print(f"  {c['family']:24s} {c['tf']:6s} {c['side']:4s} {c['regime']:16s} "
              f"n={c['n']:>6,} med={c['discovery_median']:+.2f} "
              f"P0={c['discovery_p_positive']*100:.0f}% tick={c['tickers']} "
              f"H1/H2={c['med_h1']:+.2f}/{c['med_h2']:+.2f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
