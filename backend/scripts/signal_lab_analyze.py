#!/usr/bin/env python
"""Signal Lab — анализ: система сравнений вместо одиночных рейтингов.

Срезы:
  Q  — quality matrix: engine x tf × side (signed return, медианы, P(>0/>0.5/>1),
       MFE/MAE медианы и P(MFE>=1)/P(MAE<=-1) для h=6 и h=24;
  D  — дисперсия по тикерам: engine x tf (p25/50/75, доля плюсовых тикеров);
  H  — стабильность по половинам месяца (1–15 / 16–30);
  X  — cross-TF классификация (STABLE/DECAY/INVERT/NOISY/TF-SPECIFIC);
  C  — overlap/clusters движков (содержательное пересечение сигналов);
  Z  — диагностика движков без сигналов.

Пишет CSV в reports/signal_lab/analyze/ и печатает компактные таблицы.
Запуск: python scripts/signal_lab_analyze.py --run-id <id> | --latest
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

OUT = BACKEND / "reports" / "signal_lab" / "analyze"


def _w(name: str, header: list[str], rows: list[list]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"[csv] {name}: {len(rows)} строк", flush=True)


def _rows(eng, sql: str, params: dict | None = None):
    with eng.connect() as c:
        return c.execute(text(sql), params or {}).fetchall()


def _fmt(v, nd=2):
    return "" if v is None else f"{float(v):+.{nd}f}"


def run_quality(eng, run_id: str) -> dict:
    print("[Q] quality matrix engine x tf x side (h=6/24)...", flush=True)
    q = """
      WITH base AS (
        SELECT s.strategy_id AS e, s.tf, s.side,
               o.horizon_bars AS h,
               (o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END) AS sd,
               o.mfe_atr, o.mae_atr
        FROM lab_market_outcomes o
        JOIN lab_signal_events s ON s.id = o.signal_id
        WHERE s.run_id = :r AND o.horizon_bars IN (6, 24)
      )
      SELECT e, tf, side, h, count(*) n,
             avg(sd) mean,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY sd) med,
             avg((sd > 0)::int) p0,
             avg((sd > 0.5)::int) p05,
             avg((sd > 1.0)::int) p1,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY mfe_atr) mfe50,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY mae_atr) mae50,
             avg((mfe_atr >= 1)::int) pmfe1,
             avg((mae_atr <= -1)::int) pmae1
      FROM base GROUP BY 1,2,3,4
    """
    rows = _rows(eng, q, {"r": run_id})
    out = []
    for r in rows:
        out.append([r[0], r[1], r[2], r[3], r[4], round(r[5], 4), round(r[6], 4),
                    round(r[7], 4), round(r[8], 4), round(r[9], 4),
                    round(r[10], 4), round(r[11], 4), round(r[12], 4), round(r[13], 4)])
    _w("quality_side.csv", ["engine", "tf", "side", "h", "n", "mean_signed", "med_signed",
                            "p_gt0", "p_gt05", "p_gt1", "mfe_med", "mae_med",
                            "p_mfe1", "p_mae1"], out)
    return {(r[0], r[1], r[2], r[3]): r for r in out}


def run_all_side(eng, run_id: str) -> list:
    print("[Q2] сводка без стороны (h=24)...", flush=True)
    q = """
      SELECT s.strategy_id, s.tf, count(*) n,
             avg(o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END) mean_sd,
             percentile_disc(0.5) WITHIN GROUP (
               ORDER BY o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END) med_sd,
             avg((o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END > 0.5)::int) p05,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mfe_atr) mfe50,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY o.mae_atr) mae50
      FROM lab_market_outcomes o JOIN lab_signal_events s ON s.id=o.signal_id
      WHERE s.run_id = :r AND o.horizon_bars = 24
      GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": run_id})
    out = [[r[0], r[1], r[2], round(r[3], 4), round(r[4], 4), round(r[5], 4),
            round(r[6], 4), round(r[7], 4)] for r in rows]
    _w("quality_all24.csv", ["engine", "tf", "n", "mean_signed", "med_signed",
                             "p_gt05", "mfe_med", "mae_med"], out)
    return out


def run_ticker_dispersion(eng, run_id: str) -> None:
    print("[D] дисперсия по тикерам (h=24)...", flush=True)
    q = """
      WITH per_ticker AS (
        SELECT s.strategy_id e, s.tf, s.figi,
               avg(o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END) sd
        FROM lab_market_outcomes o JOIN lab_signal_events s ON s.id=o.signal_id
        WHERE s.run_id = :r AND o.horizon_bars = 24
        GROUP BY 1,2,3
      )
      SELECT e, tf, count(*) t_n,
             avg((sd > 0)::int) pos_share,
             percentile_disc(0.25) WITHIN GROUP (ORDER BY sd) p25,
             percentile_disc(0.5) WITHIN GROUP (ORDER BY sd) p50,
             percentile_disc(0.75) WITHIN GROUP (ORDER BY sd) p75
      FROM per_ticker GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": run_id})
    out = [[r[0], r[1], r[2], round(r[3], 3), round(r[4], 3), round(r[5], 3), round(r[6], 3)]
           for r in rows]
    _w("ticker_dispersion.csv", ["engine", "tf", "tickers", "pos_ticker_share",
                                 "p25", "p50", "p75"], out)


def run_halves(eng, run_id: str) -> None:
    print("[H] половины месяца...", flush=True)
    q = """
      SELECT s.strategy_id, s.tf,
             avg(CASE WHEN s.bar_ts < '2026-09-16' THEN
                 o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END END) h1,
             avg(CASE WHEN s.bar_ts >= '2026-09-16' THEN
                 o.future_return_atr * CASE WHEN s.side='BUY' THEN 1 ELSE -1 END END) h2
      FROM lab_market_outcomes o JOIN lab_signal_events s ON s.id=o.signal_id
      WHERE s.run_id = :r AND o.horizon_bars = 24
      GROUP BY 1,2
    """
    rows = _rows(eng, q, {"r": run_id})
    out = [[r[0], r[1], round(r[2], 3) if r[2] is not None else None,
            round(r[3], 3) if r[3] is not None else None] for r in rows]
    _w("halves.csv", ["engine", "tf", "mean_first_half", "mean_second_half"], out)


def classify_tf(all24: list) -> list:
    data = defaultdict(dict)
    for e, tf, n, mean, med, p05, mfe, mae in all24:
        data[e][tf] = mean
    out = []
    for e, tfs in sorted(data.items()):
        vals = [tfs.get(t) for t in ("1min", "5min", "10min")]
        mean_v = [v for v in vals if v is not None]
        if len(mean_v) < 2:
            cls = "ONE-TF"
        else:
            signs = [(1 if v > 0.05 else (-1 if v < -0.05 else 0)) for v in vals if v is not None]
            if all(s > 0 for s in signs):
                if len(mean_v) >= 2 and abs(mean_v[0] - mean_v[-1]) < 0.15:
                    cls = "STABLE+"
                else:
                    cls = "DECAY+" if mean_v[0] >= mean_v[-1] else "GROW+"
            elif all(s < 0 for s in signs):
                cls = "STABLE-" if abs(mean_v[0] - mean_v[-1]) < 0.15 else "DECAY-"
            elif any(s > 0 for s in signs) and any(s < 0 for s in signs):
                cls = "INVERT"
            else:
                cls = "NOISY"
        out.append([e, vals[0], vals[1], vals[2], cls])
    _w("cross_tf.csv", ["engine", "mean_1m", "mean_5m", "mean_10m", "class"], out)
    return out


def run_overlap(eng, run_id: str) -> None:
    print("[C] overlap движков (загрузка сигналов)...", flush=True)
    rows = _rows(eng, "SELECT strategy_id, figi, tf, bar_ts, side "
                      "FROM lab_signal_events WHERE run_id = :r", {"r": run_id})
    print(f"    сигналов: {len(rows):,}", flush=True)
    per_engine_keys: dict[str, set] = defaultdict(set)
    keys_buf: dict[tuple, list[str]] = defaultdict(list)
    sides_buf: dict[tuple, list[str]] = defaultdict(list)
    for st, figi, tf, bar, side in rows:
        per_engine_keys[st].add((figi, tf, bar))
        keys_buf[(figi, tf, bar)].append(st)
        sides_buf[(figi, tf, bar)].append(side)
    engines = sorted(per_engine_keys)
    idx = {e: i for i, e in enumerate(engines)}
    inter = defaultdict(int)
    same_side = defaultdict(int)
    for key, sts in keys_buf.items():
        uniq = sorted(set(sts))
        side_by = {}
        for st, sd_ in zip(sts, sides_buf[key]):
            side_by.setdefault(st, sd_)
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = idx[uniq[i]], idx[uniq[j]]
                p = (a, b) if a < b else (b, a)
                inter[p] += 1
                if side_by[uniq[i]] == side_by[uniq[j]]:
                    same_side[p] += 1
    out = []
    for (a, b), x in sorted(inter.items(), key=lambda kv: -kv[1])[:200]:
        ea, eb = engines[a], engines[b]
        na, nb = len(per_engine_keys[ea]), len(per_engine_keys[eb])
        jac = x / (na + nb - x) if (na + nb - x) else 0.0
        ovl = x / min(na, nb) if min(na, nb) else 0.0
        out.append([ea, eb, x, round(ovl, 3), round(jac, 3),
                    round(same_side[(a, b)] / x, 3) if x else None])
    _w("overlap_pairs.csv", ["engine_a", "engine_b", "common_bars",
                             "overlap_coef", "jaccard", "same_side_share"], out)
    # кластеры по overlap_coef >= 0.5 (содержательные дубликаты/семейства)
    parent = {e: e for e in engines}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in out:
        if row[3] and row[3] >= 0.5:
            ra, rb = find(row[0]), find(row[1])
            if ra != rb:
                parent[rb] = ra
    clusters = defaultdict(list)
    for e in engines:
        clusters[find(e)].append(e)
    res = [sorted(v) for v in clusters.values() if len(v) > 1]
    res.sort(key=lambda v: -len(v))
    print("=== кластеры (overlap>=0.5):", flush=True)
    for c in res:
        print("   ", ", ".join(c), flush=True)


def run_zero_diag(eng, run_id: str) -> None:
    print("[Z] движки без сигналов...", flush=True)
    rows = _rows(eng, """
      SELECT e.id FROM (VALUES
        ('parabolic_sar_hub'), ('parabolic_bollinger_hub')) AS e(id)
      WHERE NOT EXISTS (
        SELECT 1 FROM lab_signal_events s
        WHERE s.run_id = :r AND s.strategy_id = e.id)
    """, {"r": run_id})
    print("   без сигналов:", [r[0] for r in rows] or "нет", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="")
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args()
    eng = engine_sync()
    with eng.connect() as c:
        run_id = args.run_id or c.execute(text(
            "SELECT id FROM lab_experiment_runs ORDER BY created_at DESC LIMIT 1")).scalar()
    print(f"run_id={run_id}", flush=True)
    run_quality(eng, run_id)
    all24 = run_all_side(eng, run_id)
    run_ticker_dispersion(eng, run_id)
    run_halves(eng, run_id)
    classify_tf(all24)
    run_zero_diag(eng, run_id)
    run_overlap(eng, run_id)
    print("готово", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
