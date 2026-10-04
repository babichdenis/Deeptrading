#!/usr/bin/env python
"""M5: event × Regime V2 × outcome.

Две версии результата (по требованию владельца):
  1) canonical-family matrix — только представители семей (family_catalog.csv),
     без дублей/сломанных — для независимого сравнения логик;
  2) full engine matrix — все движки — для диагностики реализаций/обёрток.

Regime берётся по obs_ts = start_ts события (label из measurements JSON).
Outcome — по start_signal_id, horizon=24.
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
EXCLUDED_CANON = {"canon_ensemble"}
EXCLUDED_BROKEN = {"parabolic_bollinger_hub", "parabolic_sar_hub",
                   "parabolic_price_channel_hub"}
NULL_LABEL = "(no_regime)"


def canonical_engines():
    f = OUT / "family_catalog.csv"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fh:
        return sorted({r["canonical_engine"] for r in csv.DictReader(fh)
                       if r["relationship"] == "CANONICAL"})


def query(eng, rid, engines: list[str]):
    placeholders = ",".join(f":e{i}" for i in range(len(engines)))
    params = {"r": rid}
    params.update({f"e{i}": e for i, e in enumerate(engines)})
    sql = f"""
      SELECT e.strategy_id, e.tf, e.side,
             COALESCE(r.measurements->>'label', :nl) label,
             count(*) n,
             percentile_disc(0.5) WITHIN GROUP (
               ORDER BY o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) med,
             avg((o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END > 0.5)::int) p05,
             avg((o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END > 1.0)::int) p1,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mfe_atr) mfe50,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mae_atr) mae50
      FROM lab_signal_event_runs e
      JOIN lab_market_outcomes o
        ON o.signal_id = e.start_signal_id AND o.horizon_bars = 24
      LEFT JOIN lab_regime_observations r
        ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
      WHERE e.run_id = :r AND e.strategy_id IN ({placeholders})
      GROUP BY 1,2,3,4
    """
    with eng.connect() as c:
        return c.execute(text(sql), {**params, "nl": NULL_LABEL}).fetchall()


def write_matrix(name, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    out = [[r[0], r[1], r[2], r[3], r[4], round(r[5], 4) if r[5] is not None else None,
            round(r[6], 4) if r[6] is not None else None,
            round(r[7], 4) if r[7] is not None else None,
            round(r[8], 4) if r[8] is not None else None,
            round(r[9], 4) if r[9] is not None else None] for r in rows]
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["engine", "tf", "side", "regime_label", "n", "med", "p05",
                    "p1", "mfe50", "mae50"])
        w.writerows(out)
    print(f"[csv] {name}: {len(out)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    eng = engine_sync()
    rid = args.run_id

    fam = [e for e in canonical_engines() if e not in EXCLUDED_CANON]
    print(f"canonical engines: {len(fam)} -> {fam}", flush=True)
    rows = query(eng, rid, fam)
    fam_out = write_matrix("regime_family_matrix.csv", rows)

    all_engines = [r[0] for r in eng.connect().execute(text(
        "SELECT DISTINCT strategy_id FROM lab_signal_event_runs WHERE run_id = :r"),
        {"r": rid}).fetchall()]
    all_engines = sorted(set(all_engines) - EXCLUDED_BROKEN)
    rows_full = query(eng, rid, all_engines)
    write_matrix("regime_full_matrix.csv", rows_full)

    ok_fam = [r for r in fam_out if r[4] >= 50 and r[3] != NULL_LABEL]
    ok_fam.sort(key=lambda r: -r[5])
    print("=== canonical family × regime: топ-15 по med signed @24 (n>=50)", flush=True)
    for r in ok_fam[:15]:
        print(f"  {r[0]:24s} {r[1]:6s} {r[2]:4s} {r[3]:16s} n={r[4]:>5} "
              f"med={r[5]:+.2f} P05={r[6]*100:3.0f}% P1={r[7]*100:3.0f}%", flush=True)
    bad_fam = sorted([r for r in ok_fam if r[5] < 0], key=lambda r: r[5])
    print("=== худшие-6:", flush=True)
    for r in bad_fam[:6]:
        print(f"  {r[0]:24s} {r[1]:6s} {r[2]:4s} {r[3]:16s} n={r[4]:>5} "
              f"med={r[5]:+.2f} P05={r[6]*100:3.0f}% P1={r[7]*100:3.0f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
