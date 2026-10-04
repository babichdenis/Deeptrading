#!/usr/bin/env python
"""Event-level анализ: качество на стартах событий (не на каждом баре).

События из lab_signal_event_runs; outcome берём по start_signal_id (это и есть
canonical entry point). raw-слой не используется для средних.
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


def _rows(eng, sql, params=None):
    with eng.connect() as c:
        return c.execute(text(sql), params or {}).fetchall()


def _w(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"[csv] {name}: {len(rows)}", flush=True)


def quality_side(eng, rid):
    q = """
      WITH b AS (
        SELECT e.strategy_id en, e.tf, e.side,
               o.horizon_bars h,
               (o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) sd,
               o.mfe_atr, o.mae_atr
        FROM lab_signal_event_runs e
        JOIN lab_market_outcomes o ON o.signal_id = e.start_signal_id
        WHERE e.run_id = :r AND o.horizon_bars IN (6,24)
      )
      SELECT en, tf, side, h, count(*) n, avg(sd) m,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY sd) med,
             avg((sd>0)::int) p0, avg((sd>0.5)::int) p05, avg((sd>1.0)::int) p1,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY mfe_atr) mfe50,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY mae_atr) mae50
      FROM b GROUP BY 1,2,3,4
    """
    rows = _rows(eng, q, {"r": rid})
    out = [[r[0], r[1], r[2], r[3], r[4], round(r[5], 4), round(r[6], 4),
            round(r[7], 4), round(r[8], 4), round(r[9], 4),
            round(r[10], 4), round(r[11], 4)] for r in rows]
    _w("quality_side.csv", ["engine", "tf", "side", "h", "n", "mean", "med",
                            "p0", "p05", "p1", "mfe50", "mae50"], out)


def all24(eng, rid):
    q = """
      SELECT e.strategy_id, e.tf, count(*) n,
             avg(o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) m,
             percentile_disc(0.5) WITHIN GROUP (
               ORDER BY o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) med,
             avg((o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END > 0.5)::int) p05,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mfe_atr) mfe50,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mae_atr) mae50
      FROM lab_signal_event_runs e
      JOIN lab_market_outcomes o ON o.signal_id = e.start_signal_id
      WHERE e.run_id = :r AND o.horizon_bars = 24
      GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": rid})
    out = [[r[0], r[1], r[2], round(r[3], 4), round(r[4], 4), round(r[5], 4),
            round(r[6], 4), round(r[7], 4)] for r in rows]
    _w("quality_all24.csv", ["engine", "tf", "n", "mean", "med", "p05",
                             "mfe50", "mae50"], out)
    return out


def duration(eng, rid):
    ub = {r[0]: r[1] for r in _rows(eng, """
        SELECT tf, count(DISTINCT (figi, bar_ts)) FROM lab_signal_events
        WHERE run_id = :r GROUP BY 1""", {"r": rid})}
    q = """
      SELECT strategy_id, tf, count(*) ev,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_bars) med_d,
             avg((duration_bars>=2)::int) p2, avg((duration_bars>=3)::int) p3,
             avg((duration_bars>=6)::int) p6,
             sum(duration_bars) bars, avg(signal_count) sc
      FROM lab_signal_event_runs WHERE run_id = :r GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": rid})
    out = []
    for r in rows:
        tot = int(ub.get(r[1]) or 0)
        out.append([r[0], r[1], r[2], int(r[3]), round(r[4], 3), round(r[5], 3),
                    round(r[6], 3), int(r[7]), round(r[8], 2),
                    round(r[7] / tot, 4) if tot else None])
    out.sort(key=lambda x: -x[2])
    _w("duration.csv", ["engine", "tf", "events", "med_duration", "p_ge2", "p_ge3",
                        "p_ge6", "occupied_bars", "avg_signal_count",
                        "occupied_share"], out)
    print("=== события: топ по количеству (events | med_dur | P(>=3) | занятость баров)")
    for r in out[:14]:
        print(f"  {r[0]:26s} {r[1]:6s} ev={r[2]:>6,} med={r[3]:>3} p3={r[4]*100:3.0f}% "
              f"occ={r[9]*100 if r[9] is not None else 0:5.1f}%", flush=True)
    return out


def dispersion_halves(eng, rid):
    q = """
      WITH per_ticker AS (
        SELECT e.strategy_id en, e.tf, e.figi,
               avg(o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END) sd
        FROM lab_signal_event_runs e
        JOIN lab_market_outcomes o ON o.signal_id = e.start_signal_id
        WHERE e.run_id = :r AND o.horizon_bars = 24
        GROUP BY 1,2,3
      )
      SELECT en, tf, count(*) t_n, avg((sd>0)::int) pos,
             percentile_disc(0.25) WITHIN GROUP (ORDER BY sd) p25,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY sd) p50,
             percentile_disc(0.75) WITHIN GROUP (ORDER BY sd) p75
      FROM per_ticker GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": rid})
    _w("ticker_dispersion.csv", ["engine", "tf", "tickers", "pos_share",
                                 "p25", "p50", "p75"],
       [[r[0], r[1], r[2], round(r[3], 3), round(r[4], 3), round(r[5], 3),
         round(r[6], 3)] for r in rows])
    q2 = """
      SELECT e.strategy_id, e.tf,
             avg(CASE WHEN e.start_ts < '2026-09-16' THEN
                 o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END END) h1,
             avg(CASE WHEN e.start_ts >= '2026-09-16' THEN
                 o.future_return_atr * CASE WHEN e.side='BUY' THEN 1 ELSE -1 END END) h2
      FROM lab_signal_event_runs e
      JOIN lab_market_outcomes o ON o.signal_id = e.start_signal_id
      WHERE e.run_id = :r AND o.horizon_bars = 24 GROUP BY 1,2
    """
    rows2 = _rows(eng, q2, {"r": rid})
    _w("halves.csv", ["engine", "tf", "h1", "h2"],
       [[r[0], r[1], round(r[2], 3) if r[2] is not None else None,
         round(r[3], 3) if r[3] is not None else None] for r in rows2])


def overlap(eng, rid):
    print("[C] overlap событий...", flush=True)
    rows = _rows(eng, "SELECT strategy_id, figi, tf, start_ts, side "
                      "FROM lab_signal_event_runs WHERE run_id = :r", {"r": rid})
    print(f"    событий: {len(rows):,}", flush=True)
    per = defaultdict(set)
    keys = defaultdict(list)
    for st, figi, tf, start, side in rows:
        per[st].add((figi, tf, start))
        keys[(figi, tf, start)].append((st, side))
    engines = sorted(per)
    idx = {e: i for i, e in enumerate(engines)}
    inter = defaultdict(int)
    same = defaultdict(int)
    for k, lst in keys.items():
        uniq = sorted({x[0] for x in lst})
        side_by = {}
        for st, sd in lst:
            side_by.setdefault(st, sd)
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = idx[uniq[i]], idx[uniq[j]]
                p = (a, b) if a < b else (b, a)
                inter[p] += 1
                if side_by[uniq[i]] == side_by[uniq[j]]:
                    same[p] += 1
    out = []
    for (a, b), x in sorted(inter.items(), key=lambda kv: -kv[1])[:200]:
        na, nb = len(per[engines[a]]), len(per[engines[b]])
        jac = x / (na + nb - x) if (na + nb - x) else 0
        ss = same[(a, b)] / x if x else 0
        out.append([engines[a], engines[b], x, round(x / min(na, nb), 3),
                    round(jac, 3), round(ss, 3)])
    _w("overlap_pairs.csv", ["a", "b", "common_starts", "overlap", "jaccard",
                             "same_side"], out)
    parent = {e: e for e in engines}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, x, ovl, jac, ss in out:
        if ovl >= 0.5 and ss >= 0.5:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    cl = defaultdict(list)
    for e in engines:
        cl[find(e)].append(e)
    res = [sorted(v) for v in cl.values() if len(v) > 1]
    res.sort(key=lambda v: -len(v))
    print("=== кластеры событий (overlap>=0.5, same-side>=0.5):", flush=True)
    for c in res[:8]:
        print("   ", ", ".join(c), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()
    eng = engine_sync()
    with eng.connect() as c:
        rid = args.run_id or c.execute(text(
            "SELECT id FROM lab_experiment_runs ORDER BY created_at DESC LIMIT 1")).scalar()
    print("run_id=", rid, flush=True)
    quality_side(eng, rid)
    all24(eng, rid)
    duration(eng, rid)
    dispersion_halves(eng, rid)
    overlap(eng, rid)
    print("готово", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
