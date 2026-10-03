"""Персист Signal Lab: схема create_all, upsert прогона, батч-вставка сигналов."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.models  # noqa: F401 — регистрирует модели для create_all
from app.database import Base
from app.models.signal_lab import LabExperimentRun, LabSignalEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(eng) -> None:
    Base.metadata.create_all(eng)


def upsert_run(eng, cfg, c_hash: str, code_version: str = "") -> str:
    """Создать/обновить прогон по config_hash. Возвращает run_id."""
    values = {
        "run_key": cfg.run_key,
        "config_hash": c_hash,
        "name": cfg.run_key,
        "stage": cfg.stage,
        "status": "PENDING",
        "dataset_key": cfg.dataset_key,
        "period_from": cfg.period_from,
        "period_to": cfg.period_to,
        "warmup_from": cfg.warmup_from,
        "universe": cfg.universe,
        "source_tf": "1min",
        "generated_tfs": cfg.tfs,
        "engines": cfg.engines,
        "exit_profiles": cfg.exit_profiles,
        "trailing_profiles": cfg.trailing_profiles,
        "horizons": cfg.horizons,
        "timeout_bars": cfg.timeout_bars,
        "costs": cfg.costs,
        "session_policy": {"sessions": cfg.sessions, "overnight": cfg.overnight},
        "code_version": code_version,
        "created_at": _now(),
        "updated_at": _now(),
    }
    stmt = pg_insert(LabExperimentRun).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[LabExperimentRun.config_hash],
        set_={"updated_at": stmt.excluded.updated_at, "status": "PENDING"},
    ).returning(LabExperimentRun.id)
    with eng.begin() as c:
        run_id = c.execute(stmt).scalar_one()
    return str(run_id)


def set_status(eng, run_id: str, status: str, stats: dict | None = None,
               error: str | None = None, finished: bool = False) -> None:
    sets = ["status = :s", "updated_at = :u"]
    params: dict = {"s": status, "u": _now(), "id": run_id}
    if stats is not None:
        import json as _json
        sets.append("stats = CAST(:st AS jsonb)")
        params["st"] = _json.dumps(stats, ensure_ascii=False, default=str)
    if error is not None:
        sets.append("error = :e")
        params["e"] = error[:2000]
    if finished:
        sets.append("finished_at = :u")
    with eng.begin() as c:
        c.execute(text(f"UPDATE lab_experiment_runs SET {', '.join(sets)} WHERE id = :id"), params)


def insert_signals(eng, run_id: str, rows: list[dict], chunk: int = 2000) -> int:
    """Батч-вставка сигналов с ON CONFLICT DO NOTHING (идемпотентный перезапуск)."""
    if not rows:
        return 0
    inserted = 0
    cols = {
        "run_id", "signal_uid", "figi", "ticker", "strategy_id", "strategy_version",
        "params_hash", "tf", "tf_seconds", "bar_ts", "bar_close_ts", "side", "kind",
        "reason", "features", "path_status",
    }
    with eng.begin() as c:
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            payload = []
            for r in part:
                clean = {k: r[k] for k in cols if k in r}
                clean["run_id"] = run_id
                clean.setdefault("path_status", "PENDING")
                payload.append(clean)
            stmt = pg_insert(LabSignalEvent).values(payload)
            stmt = stmt.on_conflict_do_nothing(constraint="uq_lab_signal_event")
            res = c.execute(stmt)
            inserted += res.rowcount or 0
    return inserted
