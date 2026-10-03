"""Оркестрация Signal Lab. M1: фаза signals (остальные — следующие этапы)."""
from __future__ import annotations

import multiprocessing as mp
import os
import time

from app.lab.config import LabConfig, config_hash
from app.lab.data import engine_sync, resolve_universe
from app.lab.persist import ensure_schema, insert_signals, set_status, upsert_run
from app.lab.signals import engine_task


def _code_version() -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def run_signals(cfg: LabConfig, db_url: str | None = None, jobs: int = 3,
                engines: list[str] | None = None, tickers: list[str] | None = None,
                date_from: str | None = None, date_to: str | None = None,
                dry_run: bool = False) -> dict:
    """Фаза M1: сырые сигналы всех движков по всем тикерам и ТФ в БД."""
    if engines:
        cfg.engines = list(engines)
    if date_from:
        cfg.period_from = date_from
    if date_to:
        cfg.period_to = date_to
    c_hash = config_hash(cfg, _code_version())
    eng = engine_sync(db_url)
    ensure_schema(eng)
    universe = resolve_universe(eng, tickers or cfg.universe)
    t0 = time.time()
    if dry_run:
        return {"dry_run": True, "config_hash": c_hash, "universe": len(universe),
                "engines": len(cfg.engines), "tfs": cfg.tfs}

    run_id = upsert_run(eng, cfg, c_hash, _code_version())
    set_status(eng, run_id, "SIGNALS")
    # Задачи — по (тикер × движок): тяжёлый движок не блокирует остальные,
    # прогресс и тайминги видны в логе по строкам воркеров.
    tasks = []
    for u in universe:
        for strategy_id in cfg.engines:
            tasks.append({
                "db_url": db_url,
                "figi": u["figi"], "ticker": u["ticker"],
                "strategy_id": strategy_id,
                "period_from": cfg.period_from, "period_to": cfg.period_to,
                "warmup_from": cfg.warmup_from,
                "tfs": cfg.tfs, "tf_seconds": cfg.tf_seconds,
                "engines": cfg.engines, "engine_params": cfg.engine_params,
                "window": cfg.window, "dataset_key": cfg.dataset_key,
            })

    total_rows = 0
    counts: dict = {}
    errors: list[str] = []
    slow: list[tuple[float, str, str]] = []
    done = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(engine_task, tasks):
            done += 1
            if res.get("error"):
                errors.append(f"{res['ticker']}/{res['strategy_id']}: {res['error']}")
            rows = res.get("rows") or []
            total_rows += insert_signals(eng, run_id, rows)
            for tf, n in (res.get("counts") or {}).items():
                counts[tf] = counts.get(tf, 0) + int(n)
            if float(res.get("sec") or 0) > 30:
                slow.append((float(res["sec"]), res["ticker"], res["strategy_id"]))
            if done % 40 == 0 or done == len(tasks):
                print(f"--- прогресс: {done}/{len(tasks)} задач, строк {total_rows}, "
                      f"ошибок {len(errors)}", flush=True)

    slow.sort(reverse=True)
    stats = {"signals": total_rows, "by_tf": counts, "tasks": len(tasks),
             "errors": errors[:20],
             "slowest": [{"sec": s, "ticker": t, "engine": e} for s, t, e in slow[:15]],
             "wall_sec": round(time.time() - t0, 1)}
    status = "SIGNALS_DONE" if not errors else "SIGNALS_PARTIAL"
    set_status(eng, run_id, status, stats=stats, error="; ".join(errors[:5]) or None,
               finished=True)
    return {"run_id": run_id, "config_hash": c_hash, **stats}


def run_phase(cfg: LabConfig, phase: str, **kwargs) -> dict:
    if phase == "signals":
        return run_signals(cfg, **kwargs)
    raise NotImplementedError(f"фаза {phase} — следующий этап (M2+)")
