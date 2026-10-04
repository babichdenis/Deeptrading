"""Оркестрация Signal Lab. M1: signals; M2: outcomes (остальные — следующие этапы)."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time

from sqlalchemy import text

from app.lab.config import LabConfig, config_hash
from app.lab.data import engine_sync, resolve_universe
from app.lab.persist import ensure_schema, insert_signals, set_status, upsert_run


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
                run_id: str | None = None, dry_run: bool = False, **_) -> dict:
    """Фаза M1: сырые сигналы всех движков по всем тикерам и ТФ в БД."""
    from app.lab.signals import engine_task  # ленивый импорт: signals тянет t_tech (нужен только этой фазе)
    args_run_id = bool(run_id)
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

    if run_id:
        # Добэкофилл в существующий прогон (например, после фикса движка):
        run_id = str(run_id)
        set_status(eng, run_id, "SIGNALS")
    else:
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
                # per-engine ТФ (напр. тяжёлые OSE — только 10min); кэш баров
                # в воркере всегда строит полный all_tfs, чтобы не перечитывать 1m
                "tfs": list(cfg.engine_tfs.get(strategy_id) or cfg.tfs),
                "all_tfs": cfg.tfs,
                "tf_seconds": cfg.tf_seconds,
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
             "wall_sec": round(time.time() - t0, 1),
             "backfill": bool(run_id and args_run_id)}
    if args_run_id:
        # merge со старой статистикой прогона (не терять итоги основной фазы)
        try:
            with eng.connect() as c:
                import json as _json
                old = c.execute(text("SELECT stats FROM lab_experiment_runs WHERE id=:id"),
                                {"id": run_id}).scalar()
            old = _json.loads(old) if isinstance(old, str) else (old or {})
            merged = dict(old)
            merged["signals"] = int(old.get("signals") or 0) + total_rows
            bt = dict(old.get("by_tf") or {})
            for k, v in counts.items():
                bt[k] = int(bt.get(k) or 0) + int(v)
            merged["by_tf"] = bt
            merged["errors"] = list(old.get("errors") or []) + errors[:20]
            merged["backfill_runs"] = int(old.get("backfill_runs") or 0) + 1
            merged["wall_sec"] = round(float(old.get("wall_sec") or 0) + (time.time() - t0), 1)
            stats = merged
        except Exception:
            pass
    status = "SIGNALS_DONE" if not errors else "SIGNALS_PARTIAL"
    set_status(eng, run_id, status, stats=stats, error="; ".join(errors[:5]) or None,
               finished=True)
    return {"run_id": run_id, "config_hash": c_hash, **stats}


def run_outcomes(cfg: LabConfig, db_url: str | None = None, jobs: int = 3,
                 run_id: str | None = None, tickers: list[str] | None = None,
                 dry_run: bool = False, **_) -> dict:
    """Фаза M2: горизонты + MFE/MAE для сигналов прогона (по торговому пути).

    run_id не задан — берём последний прогон со завершённой фазой signals.
    """
    from app.lab.outcomes import outcome_cfg_hash_impl, outcomes_task, persist_outcomes
    eng = engine_sync(db_url)
    ensure_schema(eng)
    if not run_id:
        with eng.connect() as c:
            run_id = c.execute(text(
                "SELECT id FROM lab_experiment_runs "
                "WHERE status IN ('SIGNALS_DONE','SIGNALS_PARTIAL','OUTCOMES_DONE','OUTCOMES_PARTIAL') "
                "ORDER BY created_at DESC LIMIT 1")).scalar()
    if not run_id:
        raise SystemExit("нет прогона с сигналами (--run-id?)")
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT figi, ticker FROM lab_signal_events WHERE run_id = :r"
        ), {"r": str(run_id)}).fetchall()
    if tickers:
        want = {t.upper() for t in tickers}
        rows = [r for r in rows if str(r[1]).upper() in want]
    # Пропускаем figi, где исходы уже посчитаны (перезапуск после сбоя не пересчитывает
    # готовое): n_ok сигналов с anchor × len(horizons) строк = завершено.
    done_figis: set[str] = set()
    if not dry_run:
        with eng.connect() as c:
            ok_map = dict(c.execute(text(
                "SELECT figi, count(*) FROM lab_signal_events "
                "WHERE run_id = :r AND path_status = 'OK' GROUP BY figi"
            ), {"r": str(run_id)}).fetchall())
            out_map = dict(c.execute(text(
                "SELECT s.figi, count(DISTINCT o.signal_id) FROM lab_market_outcomes o "
                "JOIN lab_signal_events s ON s.id = o.signal_id "
                "WHERE s.run_id = :r GROUP BY s.figi"
            ), {"r": str(run_id)}).fetchall())
        # n_out — число сигналов с исходами (не строк): полный figi = все OK-сигналы
        done_figis = {f for f, n in ok_map.items()
                      if int(n) > 0 and int(out_map.get(f, 0)) >= int(n)}
        rows = [r for r in rows if r[0] not in done_figis]
        print(f"--- пропущено готовых figi: {len(done_figis)}; в работе: {len(rows)}", flush=True)
    if dry_run:
        return {"dry_run": True, "run_id": str(run_id), "figis": len(rows)}
    cfg_h = outcome_cfg_hash_impl(cfg.horizons, cfg.atr_period, cfg.sessions, impl="numpy")
    tasks = [{
        "db_url": db_url, "run_id": str(run_id), "figi": r[0], "ticker": r[1],
        "horizons": cfg.horizons, "atr_period": cfg.atr_period, "cfg_hash": cfg_h,
        "persist": True,
    } for r in rows]

    t0 = time.time()
    n_ins = n_upd = 0
    _persist_sec = 0.0
    errors: list[str] = []
    done = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(outcomes_task, tasks):
            done += 1
            if res.get("error"):
                errors.append(f"{res.get('ticker')}: {res['error']}")
            if res.get("n_ins") is not None or res.get("n_upd") is not None:
                _u = int(res.get("n_upd") or 0)
                _i = int(res.get("n_ins") or 0)
            else:
                _tp = time.time()
                _u, _i = persist_outcomes(eng, res)
                _persist_sec += time.time() - _tp
            n_upd += _u
            n_ins += _i
            print(f"[{done}/{len(tasks)}] {res.get('ticker')}: outcomes={_i} "
                  f"updates={_u} skip={res.get('skipped')} load={res.get('sec_load')}s "
                  f"compute={res.get('sec_compute')}s "
                  f"persist(u/i)={res.get('persist_upd_sec')}/{res.get('persist_ins_sec')}s "
                  f"{res.get('error') or ''}", flush=True)

    stats = {"outcomes": n_ins, "signals_updated": n_upd, "figis": len(tasks),
             "errors": errors[:20], "wall_sec": round(time.time() - t0, 1),
             "persist_sec": round(_persist_sec, 1)}
    status = "OUTCOMES_DONE" if not errors else "OUTCOMES_PARTIAL"
    set_status(eng, str(run_id), status, stats=stats,
               error="; ".join(errors[:5]) or None, finished=True)
    return {"run_id": str(run_id), "cfg_hash": cfg_h, **stats}


def run_fixed(cfg: LabConfig, db_url: str | None = None, jobs: int = 3,
              run_id: str | None = None, tickers: list[str] | None = None,
              dry_run: bool = False, **_) -> dict:
    """Фаза M3: 4 профиля fixed SL/TP по торговому пути (worker пишет сам, COPY)."""
    from app.lab.fixed import fixed_cfg_hash, fixed_task
    eng = engine_sync(db_url)
    ensure_schema(eng)
    if not run_id:
        with eng.connect() as c:
            run_id = c.execute(text(
                "SELECT id FROM lab_experiment_runs "
                "WHERE status IN ('SIGNALS_DONE','SIGNALS_PARTIAL','OUTCOMES_DONE','OUTCOMES_PARTIAL',"
                "'FIXED_DONE','FIXED_PARTIAL') ORDER BY created_at DESC LIMIT 1")).scalar()
    if not run_id:
        raise SystemExit("нет прогона с сигналами (--run-id?)")
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT figi, ticker FROM lab_signal_events WHERE run_id = :r"
        ), {"r": str(run_id)}).fetchall()
    if tickers:
        want = {t.upper() for t in tickers}
        rows = [r for r in rows if str(r[1]).upper() in want]
    if dry_run:
        return {"dry_run": True, "run_id": str(run_id), "figis": len(rows)}
    cfg_h = fixed_cfg_hash(cfg.exit_profiles, cfg.timeout_bars, cfg.atr_period,
                           cfg.sessions, cfg.costs)
    tasks = [{
        "db_url": db_url, "run_id": str(run_id), "figi": r[0], "ticker": r[1],
        "profiles": cfg.exit_profiles, "timeout_bars": cfg.timeout_bars,
        "atr_period": cfg.atr_period, "costs": cfg.costs, "cfg_hash": cfg_h,
    } for r in rows]

    t0 = time.time()
    n_rows = 0
    reasons: dict = {}
    errors: list[str] = []
    done = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(fixed_task, tasks):
            done += 1
            if res.get("error"):
                errors.append(f"{res.get('ticker')}: {res['error']}")
            n_rows += int(res.get("rows") or 0)
            for k, v in (res.get("reasons") or {}).items():
                reasons[k] = reasons.get(k, 0) + int(v)
            print(f"[{done}/{len(tasks)}] {res.get('ticker')}: rows={res.get('rows')} "
                  f"no_atr={res.get('no_atr')} {res.get('reasons')} {res.get('error') or ''}",
                  flush=True)
    stats = {"fixed_rows": n_rows, "reasons": reasons, "figis": len(tasks),
             "errors": errors[:20], "wall_sec": round(time.time() - t0, 1)}
    status = "FIXED_DONE" if not errors else "FIXED_PARTIAL"
    set_status(eng, str(run_id), status, stats=stats,
               error="; ".join(errors[:5]) or None, finished=True)
    return {"run_id": str(run_id), "cfg_hash": cfg_h, **stats}


def run_trailing(cfg: LabConfig, db_url: str | None = None, jobs: int = 3,
                 run_id: str | None = None, tickers: list[str] | None = None,
                 dry_run: bool = False, **_) -> dict:
    """Фаза M4: trailing-модели (T1–T3) по торговому пути; worker пишет COPY."""
    from app.lab.trailing import trailing_cfg_hash, trailing_task
    eng = engine_sync(db_url)
    ensure_schema(eng)
    if not run_id:
        with eng.connect() as c:
            run_id = c.execute(text(
                "SELECT id FROM lab_experiment_runs "
                "WHERE status LIKE 'FIXED%' OR status LIKE 'TRAILING%' OR status LIKE 'OUTCOMES%' "
                "ORDER BY created_at DESC LIMIT 1")).scalar()
    if not run_id:
        raise SystemExit("нет прогона с fixed/outcomes (--run-id?)")
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT figi, ticker FROM lab_signal_events WHERE run_id = :r"
        ), {"r": str(run_id)}).fetchall()
    if tickers:
        want = {t.upper() for t in tickers}
        rows = [r for r in rows if str(r[1]).upper() in want]
    # пропуск готовых figi (distinct сигналов с trailing >= OK-сигналов)
    done_figis: set[str] = set()
    if not dry_run:
        with eng.connect() as c:
            ok_map = dict(c.execute(text(
                "SELECT figi, count(*) FROM lab_signal_events "
                "WHERE run_id = :r AND path_status = 'OK' GROUP BY figi"
            ), {"r": str(run_id)}).fetchall())
            out_map = dict(c.execute(text(
                "SELECT s.figi, count(DISTINCT t.signal_id) FROM lab_trailing_results t "
                "JOIN lab_signal_events s ON s.id = t.signal_id "
                "WHERE s.run_id = :r GROUP BY s.figi"
            ), {"r": str(run_id)}).fetchall())
        done_figis = {f for f, n in ok_map.items()
                      if int(n) > 0 and int(out_map.get(f, 0)) >= int(n)}
        rows = [r for r in rows if r[0] not in done_figis]
        print(f"--- пропущено готовых figi: {len(done_figis)}; в работе: {len(rows)}", flush=True)
    if dry_run:
        return {"dry_run": True, "run_id": str(run_id), "figis": len(rows)}
    cfg_h = trailing_cfg_hash(cfg.trailing_profiles, cfg.timeout_bars, cfg.atr_period,
                              cfg.sessions, cfg.costs)
    tasks = [{
        "db_url": db_url, "run_id": str(run_id), "figi": r[0], "ticker": r[1],
        "models": cfg.trailing_profiles, "timeout_bars": cfg.timeout_bars,
        "atr_period": cfg.atr_period, "costs": cfg.costs, "cfg_hash": cfg_h,
    } for r in rows]

    t0 = time.time()
    n_rows = 0
    by_model: dict = {}
    errors: list[str] = []
    done = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(trailing_task, tasks):
            done += 1
            if res.get("error"):
                errors.append(f"{res.get('ticker')}: {res['error']}")
            n_rows += int(res.get("rows") or 0)
            for k, v in (res.get("by_model") or {}).items():
                by_model[k] = by_model.get(k, 0) + int(v)
            print(f"[{done}/{len(tasks)}] {res.get('ticker')}: rows={res.get('rows')} "
                  f"{res.get('by_model')} skip={res.get('skipped_models')} {res.get('error') or ''}",
                  flush=True)
    stats = {"trailing_rows": n_rows, "by_model": by_model, "figis": len(tasks),
             "errors": errors[:20], "wall_sec": round(time.time() - t0, 1)}
    status = "TRAILING_DONE" if not errors else "TRAILING_PARTIAL"
    set_status(eng, str(run_id), status, stats=stats,
               error="; ".join(errors[:5]) or None, finished=True)
    return {"run_id": str(run_id), "cfg_hash": cfg_h, **stats}


def run_events(cfg: LabConfig, db_url: str | None = None, jobs: int = 4,
               run_id: str | None = None, tickers: list[str] | None = None,
               dry_run: bool = False, **_) -> dict:
    """Фаза events: события = серии одинаковых сигналов (без чистки raw)."""
    from app.lab.events import events_task
    eng = engine_sync(db_url)
    ensure_schema(eng)
    if not run_id:
        with eng.connect() as c:
            run_id = c.execute(text(
                "SELECT id FROM lab_experiment_runs ORDER BY created_at DESC LIMIT 1")).scalar()
    if not run_id:
        raise SystemExit("нет прогона (--run-id?)")
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT figi, ticker FROM lab_signal_events WHERE run_id = :r"
        ), {"r": str(run_id)}).fetchall()
        done = {r[0] for r in c.execute(text(
            "SELECT DISTINCT figi FROM lab_signal_event_runs WHERE run_id = :r"),
            {"r": str(run_id)}).fetchall()}
    if tickers:
        want = {t.upper() for t in tickers}
        rows = [r for r in rows if str(r[1]).upper() in want]
    rows = [r for r in rows if r[0] not in done]
    print(f"--- готовых figi пропущено: {len(done)}; в работе: {len(rows)}", flush=True)
    if dry_run:
        return {"dry_run": True, "run_id": str(run_id), "figis": len(rows)}
    tasks = [{"db_url": db_url, "run_id": str(run_id), "figi": r[0], "ticker": r[1]}
             for r in rows]
    t0 = time.time()
    n_ev = 0
    errors: list[str] = []
    done_n = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(events_task, tasks):
            done_n += 1
            if res.get("error"):
                errors.append(f"{res.get('ticker')}: {res['error']}")
            n_ev += int(res.get("events") or 0)
            print(f"[{done_n}/{len(tasks)}] {res.get('ticker')}: events={res.get('events')} "
                  f"{res.get('error') or ''}", flush=True)
    stats = {"events": n_ev, "figis": len(tasks), "errors": errors[:20],
             "wall_sec": round(time.time() - t0, 1)}
    status = "EVENTS_DONE" if not errors else "EVENTS_PARTIAL"
    set_status(eng, str(run_id), status, stats=stats,
               error="; ".join(errors[:5]) or None, finished=True)
    return {"run_id": str(run_id), **stats}


def run_regime(cfg: LabConfig, db_url: str | None = None, jobs: int = 4,
               tickers: list[str] | None = None, dry_run: bool = False, **_) -> dict:
    """M5: Regime V2 по тикерам и ТФ прогона → lab_regime_observations."""
    from app.lab.regime import regime_task
    eng = engine_sync(db_url)
    ensure_schema(eng)
    universe = resolve_universe(eng, tickers or cfg.universe)
    if dry_run:
        return {"dry_run": True, "figis": len(universe), "tfs": cfg.tfs}
    tasks = [{
        "db_url": db_url, "figi": u["figi"], "ticker": u["ticker"],
        "period_from": cfg.period_from, "period_to": cfg.period_to,
        "warmup_from": cfg.warmup_from, "tfs": cfg.tfs,
        "tf_seconds": cfg.tf_seconds,
    } for u in universe]
    t0 = time.time()
    total = 0
    errors: list[str] = []
    done = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, int(jobs))) as pool:
        for res in pool.imap_unordered(regime_task, tasks):
            done += 1
            if res.get("error"):
                errors.append(f"{res.get('ticker')}: {res['error']}")
            n = sum(res.get("by_tf", {}).values())
            total += n
            print(f"[{done}/{len(tasks)}] {res.get('ticker')}: obs={n} "
                  f"{res.get('by_tf')} {res.get('error') or ''}", flush=True)
    stats = {"obs": total, "figis": len(tasks), "errors": errors[:20],
             "wall_sec": round(time.time() - t0, 1)}
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    return stats


def run_phase(cfg: LabConfig, phase: str, **kwargs) -> dict:
    if phase == "signals":
        return run_signals(cfg, **kwargs)
    if phase == "outcomes":
        return run_outcomes(cfg, **kwargs)
    if phase == "fixed":
        return run_fixed(cfg, **kwargs)
    if phase == "trailing":
        return run_trailing(cfg, **kwargs)
    if phase == "events":
        return run_events(cfg, **kwargs)
    if phase == "regime":
        return run_regime(cfg, **kwargs)
    raise NotImplementedError(f"фаза {phase} — следующий этап (M5)")
