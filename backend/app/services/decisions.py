from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signals import SignalDecision, StrategyRun, StrategySignal
from app.services.tinvest import INTERVAL_NAMES

POLICY_IGNORE_SAME_SIDE = "ignore_same_side"


def policy_params_hash(policy_id: str, params: dict) -> str:
    canonical = json.dumps(
        {"policy_id": policy_id, **(params or {})}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def compute_decisions(
    db: AsyncSession,
    run_id: uuid.UUID,
    policy_id: str = POLICY_IGNORE_SAME_SIDE,
    min_hold_bars: int = 0,
) -> dict:
    run = await db.scalar(select(StrategyRun).where(StrategyRun.id == run_id))
    if run is None:
        raise ValueError("run not found")
    if policy_id != POLICY_IGNORE_SAME_SIDE:
        raise ValueError(f"unknown policy: {policy_id}")

    params = {"min_hold_bars": min_hold_bars}
    p_hash = policy_params_hash(policy_id, params)

    rows = await db.execute(
        select(StrategySignal).where(StrategySignal.run_id == run_id).order_by(StrategySignal.ts)
    )
    signals = rows.scalars().all()

    interval = INTERVAL_NAMES.get(run.interval_name)
    step_sec = int(getattr(getattr(interval, "value", interval), "value", 300)) if interval else 300
    hold_delta = (
        timedelta(seconds=step_sec * min_hold_bars) if min_hold_bars > 0 else timedelta(seconds=0)
    )

    state = "FLAT"
    state_since: datetime | None = None
    counts = {"ACCEPTED": 0, "IGNORED": 0, "REJECTED": 0}
    results: list[dict] = []

    for sig in signals:
        before = state
        side = sig.side
        same_side = (state == "LONG" and side == "BUY") or (state == "SHORT" and side == "SELL")
        if state == "FLAT":
            decision, reason_code = "ACCEPTED", "entry"
            state = "LONG" if side == "BUY" else "SHORT"
            state_since = sig.ts
        elif same_side:
            decision, reason_code = "IGNORED", "ignore_same_side"
        elif sig.ts - (state_since or sig.ts) < hold_delta:
            decision, reason_code = "REJECTED", "min_hold_bars"
        else:
            decision, reason_code = "ACCEPTED", "opposite_exit"
            state = "FLAT"
            state_since = None
        counts[decision] += 1
        results.append(
            {
                "signal_id": sig.id,
                "ts": sig.ts.isoformat(),
                "side": side,
                "reason": sig.reason,
                "decision": decision,
                "reason_code": reason_code,
                "details": {"position_state_before": before, "position_state_after": state},
            }
        )

    await db.execute(
        delete(SignalDecision).where(
            SignalDecision.policy_id == policy_id,
            SignalDecision.params_hash == p_hash,
            SignalDecision.signal_id.in_(
                select(StrategySignal.id).where(StrategySignal.run_id == run_id)
            ),
        )
    )
    for r in results:
        db.add(
            SignalDecision(
                signal_id=r["signal_id"],
                decision=r["decision"],
                policy_id=policy_id,
                params_hash=p_hash,
                reason_code=r["reason_code"],
                details=r["details"],
            )
        )
    await db.commit()

    return {
        "run_id": str(run_id),
        "policy_id": policy_id,
        "params": params,
        "total": len(results),
        **counts,
        "decisions": results,
    }
