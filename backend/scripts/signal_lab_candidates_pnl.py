#!/usr/bin/env python
"""Frozen-кандидаты → PnL после издержек (fixed SL/TP и trailing симуляторы).

Без оптимизации: заранее замороженные ячейки (frozen_selected.csv / frozen_cells.csv),
net_return уже включает комиссию и слиппедж (CostModel).

Защита от мусора:
  - строки с entry_px <= 1.0 (сентинел) исключаются;
  - winsorize net_return ±20% для агрегатов (шум данных);
  - доля топ-тикера в ячейке (концентрация).
Метрики на ячейку × профиль: сделок, winrate, avg/med net, avg R, raw и winsor
сумма net, макс. просадка.
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
WINSOR = 0.20


def load_cells(path: str, only_selected: bool):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if only_selected:
        rows = [r for r in rows if r.get("selected", "True") == "True"]
    keys = {}
    for r in rows:
        keys.setdefault(r["family"], set()).add((r["tf"], r["side"], r["regime"]))
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--mode", choices=["fixed", "trailing"], required=True)
    ap.add_argument("--cells", default=str(OUT / "frozen_selected.csv"))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    eng = engine_sync()
    keys = load_cells(args.cells, only_selected=not args.all)
    engines = sorted(keys)
    ph = ",".join(f":e{i}" for i in range(len(engines)))
    params = {"r": args.run_id, "nl": NULL_LABEL}
    params.update({f"e{i}": e for i, e in enumerate(engines)})

    alias = "f" if args.mode == "fixed" else "t"
    prof_col = f"{alias}.{'profile_code' if args.mode == 'fixed' else 'model_code'}"
    table = "fixed_exits" if args.mode == "fixed" else "trailing_results"
    join_where = (f"JOIN lab_{table} {alias} ON {alias}.signal_id = e.start_signal_id "
                  f"AND {alias}.entry_px > 1.0")
    lj = ("LEFT JOIN lab_regime_observations r ON r.figi = e.figi "
          "AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts")

    sql = f"""
      SELECT e.strategy_id, e.tf, e.side, COALESCE(r.measurements->>'label', :nl) label,
             {prof_col} profile, count(*) n,
             avg(({alias}.net_return > 0)::int) win,
             avg({alias}.net_return) avg_net,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY {alias}.net_return) med_net,
             avg({alias}.r_multiple) avg_r,
             sum({alias}.net_return) sum_net,
             avg(LEAST(GREATEST({alias}.net_return, -{WINSOR}), {WINSOR})) avg_net_w,
             sum(LEAST(GREATEST({alias}.net_return, -{WINSOR}), {WINSOR})) sum_net_w,
             sum((abs({alias}.net_return) > {WINSOR})::int) clipped
      FROM lab_signal_event_runs e {join_where} {lj}
      WHERE e.run_id = :r AND e.strategy_id IN ({ph})
      GROUP BY 1,2,3,4,5
    """
    agg = {}
    for r in eng.connect().execute(text(sql), params):
        if (r[1], r[2], r[3]) not in keys.get(r[0], set()):
            continue
        agg[(r[0], r[1], r[2], r[3], r[4])] = {
            "n": int(r[5]), "win": float(r[6] or 0), "avg_net": float(r[7] or 0),
            "med_net": float(r[8] or 0), "avg_r": float(r[9] or 0),
            "sum_net": float(r[10] or 0), "avg_net_w": float(r[11] or 0),
            "sum_net_w": float(r[12] or 0), "clipped": int(r[13] or 0)}

    conc_sql = f"""
      SELECT e.strategy_id, e.tf, e.side, COALESCE(r.measurements->>'label', :nl) label,
             {prof_col} profile, e.ticker, count(*) n
      FROM lab_signal_event_runs e {join_where} {lj}
      WHERE e.run_id = :r AND e.strategy_id IN ({ph})
      GROUP BY 1,2,3,4,5,6
    """
    top_share: dict = {}
    tot: dict = {}
    for r in eng.connect().execute(text(conc_sql), params):
        if (r[1], r[2], r[3]) not in keys.get(r[0], set()):
            continue
        k = (r[0], r[1], r[2], r[3], r[4])
        tot[k] = tot.get(k, 0) + int(r[6])
        if int(r[6]) > top_share.get(k, ("", 0))[1]:
            top_share[k] = (r[5], int(r[6]))

    dd_sql = f"""
      SELECT e.strategy_id, e.tf, e.side, COALESCE(r.measurements->>'label', :nl) label,
             {prof_col} profile, {alias}.net_return
      FROM lab_signal_event_runs e {join_where} {lj}
      WHERE e.run_id = :r AND e.strategy_id IN ({ph})
      ORDER BY e.strategy_id, e.tf, e.side, profile, e.start_ts
    """
    cur, dd = {}, {}
    for r in eng.connect().execute(text(dd_sql), params):
        k = (r[0], r[1], r[2], r[3], r[4])
        if (r[1], r[2], r[3]) not in keys.get(r[0], set()):
            continue
        x = min(max(float(r[5] or 0), -WINSOR), WINSOR)
        s = cur.get(k, (0.0, 0.0))
        cum = s[0] + x
        peak = max(s[1], cum)
        dd[k] = max(dd.get(k, 0.0), peak - cum)
        cur[k] = (cum, peak)

    rows = []
    for k, v in agg.items():
        t = top_share.get(k, ("", 0))
        rows.append([k[0], k[1], k[2], k[3], k[4], v["n"], round(v["win"], 3),
                     round(v["avg_net"] * 100, 4), round(v["med_net"] * 100, 4),
                     round(v["avg_r"], 3), round(v["sum_net"] * 100, 2),
                     round(v["sum_net_w"] * 100, 2), v["clipped"],
                     round(dd.get(k, 0.0) * 100, 2),
                     t[0], round(t[1] / max(tot.get(k, 1), 1), 3)])
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    name = args.out or f"candidates_pnl_{args.mode}.csv"
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["engine", "tf", "side", "regime", "profile", "n", "win",
                    "avg_net_pct", "med_net_pct", "avg_r", "sum_net_pct",
                    "sum_net_w_pct", "clipped", "dd_pct_pp",
                    "top_ticker", "top_ticker_share"])
        w.writerows(rows)
    print(f"[csv] {name}: {len(rows)} строк", flush=True)

    show = [r for r in rows if r[5] >= 30]
    show.sort(key=lambda x: -x[11])
    print(f"=== {args.mode}: топ-15 по winsor sumNet (n>=30, 1 юнит):", flush=True)
    for r in show[:15]:
        print(f"  {r[0]:20s} {r[1]:5s} {r[2]:4s} {r[3]:15s} {r[4]:10s} n={r[5]:>5} "
              f"win={r[6]*100:3.0f}% avgN={r[7]:+.3f}% medN={r[8]:+.3f}% "
              f"sumW={r[11]:+.1f}% DD={r[13]:.1f}пп top={r[14]}:{r[15]*100:.0f}%",
              flush=True)
    print(f"=== {args.mode}: анти-топ-8:", flush=True)
    for r in show[-8:][::-1]:
        print(f"  {r[0]:20s} {r[1]:5s} {r[2]:4s} {r[3]:15s} {r[4]:10s} n={r[5]:>5} "
              f"win={r[6]*100:3.0f}% avgN={r[7]:+.3f}% sumW={r[11]:+.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
