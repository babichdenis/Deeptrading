from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.catalog import canonical_params_hash
from app.engine.version import ENGINE_ID
from app.models.signals import RunDependency, StrategyRun, StrategySignal


async def compute_quorum(db: AsyncSession, member_run_ids: list[uuid.UUID], k: int) -> dict:
    if len(set(member_run_ids)) != len(member_run_ids):
        raise ValueError("duplicate member run_ids")
    if len(member_run_ids) < 2:
        raise ValueError("need at least 2 member runs")
    if k < 1 or k > len(member_run_ids):
        raise ValueError(f"k must be in 1..{len(member_run_ids)}")

    runs = (
        await db.execute(select(StrategyRun).where(StrategyRun.id.in_(member_run_ids)))
    ).scalars().all()
    by_id = {r.id: r for r in runs}
    missing = [str(r) for r in member_run_ids if r not in by_id]
    if missing:
        raise ValueError(f"runs not found: {missing}")

    first = by_id[member_run_ids[0]]
    figi = first.figi
    interval_name = first.interval_name
    for r in runs:
        if r.figi != figi or r.interval_name != interval_name:
            raise ValueError("member runs must share the same figi and interval")

    member_runs = []
    for rid in member_run_ids:
        sigs = (await db.execute(
            select(StrategySignal).where(StrategySignal.run_id == rid).order_by(StrategySignal.ts)
        )).scalars().all()
        member_runs.append((by_id[rid].strategy_id, [{"ts": x.ts, "side": x.side} for x in sigs]))

    from app.engine.quorum import merge_quorum
    merged, _funnel = merge_quorum(member_runs, k)
    quorum_signals = [
        {
            "ts": m["ts"],
            "side": m["side"],
            "status": "CANDIDATE",
            "reason": m["reason"],
            "features": m["features"],
        }
        for m in merged
    ]

    params = {"members": [str(r) for r in member_run_ids], "k": k}
    p_hash = canonical_params_hash(params)
    data_ver = "|".join(
        sorted(by_id[rid].data_version for rid in member_run_ids)
    )
    combined_data_hash = __import__("hashlib").sha256(data_ver.encode()).hexdigest()[:16]

    existing = await db.scalar(
        select(StrategyRun).where(
            StrategyRun.figi == figi,
            StrategyRun.interval_name == interval_name,
            StrategyRun.strategy_id == "quorum",
            StrategyRun.params_hash == p_hash,
            StrategyRun.engine_version == ENGINE_ID,
        )
    )
    replaced_stale = False
    if existing is not None:
        if existing.data_version == combined_data_hash:
            sigs = await _load_quorum_signals(db, existing.id)
            return {
                "run_id": str(existing.id),
                "cached": True,
                "replaced_stale": False,
                "count": len(sigs),
                "signals": sigs,
            }
        await db.delete(existing)
        await db.flush()
        replaced_stale = True

    run = StrategyRun(
        figi=figi,
        interval_name=interval_name,
        strategy_id="quorum",
        strategy_version="1.0.0",
        engine_version=ENGINE_ID,
        data_version=combined_data_hash,
        params=params,
        params_hash=p_hash,
        bars=max(r.bars for r in runs),
        from_ts=min(r.from_ts for r in runs),
        to_ts=max(r.to_ts for r in runs),
    )
    db.add(run)
    await db.flush()

    for s in quorum_signals:
        db.add(StrategySignal(run_id=run.id, figi=figi, **s))
    for rid in member_run_ids:
        db.add(RunDependency(parent_run_id=run.id, child_run_id=rid, role="member"))
    await db.commit()

    return {
        "run_id": str(run.id),
        "cached": False,
        "replaced_stale": replaced_stale,
        "count": len(quorum_signals),
        "signals": [
            {**s, "ts": s["ts"].isoformat(), "features": dict(s["features"])}
            for s in quorum_signals
        ],
    }


async def _load_quorum_signals(db: AsyncSession, run_id) -> list[dict]:
    rows = await db.execute(
        select(StrategySignal).where(StrategySignal.run_id == run_id).order_by(StrategySignal.ts)
    )
    return [
        {
            "ts": s.ts.isoformat(),
            "side": s.side,
            "status": s.status,
            "reason": s.reason,
            "features": s.features,
        }
        for s in rows.scalars()
    ]
