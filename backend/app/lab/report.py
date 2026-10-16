"""Полный отчёт по стандарту репозитория (MEMORY.md) для lab-прогона.

Фаза `report`: движок × ТФ × сторона × тикер × режим × сессия.
Метрики: n, wins, losses, winrate, PF, avg win/loss (₽ и R), суммы.
Weekend/night — отдельным файлом, в агрегаты не входят.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from app.lab.data import engine_sync

OUT = Path(__file__).resolve().parents[2] / "reports" / "signal_lab" / "report"
BANNED = {"weekend", "night"}


def run_report(cfg, db_url=None, run_id=None, profile="sl3_tp6", min_n=10, **_):
    eng = engine_sync(db_url)
    with eng.connect() as c:
        if not run_id:
            run_id = c.execute(text(
                "SELECT id FROM lab_experiment_runs ORDER BY created_at DESC LIMIT 1")).scalar()
        rows = c.execute(text("""
            SELECT e.strategy_id, e.tf, e.side, e.ticker,
                   COALESCE(r.measurements->>'label', '(no_regime)') AS label,
                   COALESCE(r.session, '(no_session)') AS session,
                   count(*) AS n,
                   count(*) FILTER (WHERE f.net_return > 0) AS wins,
                   count(*) FILTER (WHERE f.net_return <= 0) AS losses,
                   avg(f.net_return) AS avg_net,
                   sum(f.net_return) FILTER (WHERE f.net_return > 0) AS s_win,
                   sum(f.net_return) FILTER (WHERE f.net_return <= 0) AS s_loss,
                   avg(f.net_return * f.entry_px) FILTER (WHERE f.net_return > 0) AS avg_win_rub,
                   avg(f.net_return * f.entry_px) FILTER (WHERE f.net_return <= 0) AS avg_loss_rub,
                   avg(f.r_multiple) AS avg_r
            FROM lab_signal_event_runs e
            JOIN lab_fixed_exits f ON f.signal_id = e.start_signal_id AND f.profile_code = :p
            LEFT JOIN lab_regime_observations r
              ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
            WHERE e.run_id = :r
            GROUP BY 1,2,3,4,5,6
        """), {"r": str(run_id), "p": profile}).fetchall()
    OUT.mkdir(parents=True, exist_ok=True)
    hdr = ["engine", "tf", "side", "ticker", "regime", "session", "n", "wins",
           "losses", "winrate_pct", "pf", "avg_net_pct", "avg_win_rub",
           "avg_loss_rub", "avg_r"]
    out_rows, excluded = [], []
    for r in rows:
        n, wins, losses = int(r[6]), int(r[7]), int(r[8])
        pf = None
        if r[10] is not None and r[11] is not None and float(r[11]) != 0:
            pf = float(r[10]) / abs(float(r[11]))
        rec = [r[0], r[1], r[2], r[3], r[4], r[5], n, wins, losses,
               round(wins / n * 100, 1) if n else 0,
               round(pf, 2) if pf is not None else None,
               round(float(r[9] or 0) * 100, 4),
               round(float(r[12]), 2) if r[12] is not None else None,
               round(float(r[13]), 2) if r[13] is not None else None,
               round(float(r[14]), 3) if r[14] is not None else None]
        (excluded if str(r[5]) in BANNED or str(r[5]) == "(no_session)" else out_rows).append(rec)
    f1 = OUT / f"{run_id}_cells.csv"
    f2 = OUT / f"{run_id}_cells_excluded.csv"
    with open(f1, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(out_rows)
    with open(f2, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(excluded)
    # агрегаты
    def agg_by(key_idx, min_nn):
        g = defaultdict(lambda: [0, 0, 0.0, 0.0])
        for r in out_rows:
            k = tuple(r[i] for i in key_idx)
            a = g[k]
            a[0] += r[6]; a[1] += r[7]; a[2] += (r[11] or 0); a[3] += (r[12] or 0) * r[6]
        res = []
        for k, a in sorted(g.items()):
            n, wins = a[0], a[1]
            if n < min_nn:
                continue
            res.append([*k, n, wins, n - wins, round(wins / n * 100, 1)])
        return res
    for name, idx in (("by_engine_tf_side", (0, 1, 2)),
                      ("by_engine_ticker", (0, 3)),
                      ("by_engine_regime", (0, 4)),
                      ("by_engine_session", (0, 5))):
        with open(OUT / f"{run_id}_{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key...", "n", "wins", "losses", "winrate_pct"])
            w.writerows(agg_by(idx, min_n))
    md = OUT / f"{run_id}_summary.md"
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# Отчёт по стандарту · run {run_id} · профиль {profile}\n\n")
        f.write(f"Ячеек (без weekend/night): {len(out_rows)}; исключено: {len(excluded)}.\n\n")
        top = [r for r in out_rows if r[6] >= min_n and r[10] is not None]
        top.sort(key=lambda r: -r[10])
        f.write("## Топ-20 по PF (n>={min_n})\n\n")
        f.write("|engine|tf|side|ticker|regime|session|n|WR%|PF|avg₽win|avg₽loss|\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in top[:20]:
            f.write(f"|{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}|{r[6]}|"
                    f"{r[9]}|{r[10]}|{r[12]}|{r[13]}|\n")
        f.write("\n## Дно-10 по PF\n\n")
        for r in top[-10:]:
            f.write(f"|{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}|{r[6]}|"
                    f"{r[9]}|{r[10]}|{r[12]}|{r[13]}|\n")
    return {"run_id": str(run_id), "cells": len(out_rows), "excluded": len(excluded),
            "files": str(OUT)}
