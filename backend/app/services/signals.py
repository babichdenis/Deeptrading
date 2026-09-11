from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.catalog import STRATEGY_CATALOG, canonical_params_hash
from app.engine.models import Candle as EngineCandle
from app.engine.strategies import ParamValidationError, build_strategy, validate_params
from app.engine.version import ENGINE_ID
from app.engine.views import CandleWindow
from app.models.candle import Candle
from app.models.signals import RunDependency, StrategyRun, StrategySignal
from app.services.candle_cache import DEFAULT_DAYS, ensure_candles
from app.services.tinvest import INTERVAL_NAMES


async def _load_candles(db: AsyncSession, figi: str, interval_value: int,
                        date_from=None, date_to=None) -> list[EngineCandle]:
    stmt = select(Candle).where(
        Candle.figi == figi, Candle.interval == interval_value
    )
    if date_from is not None:
        stmt = stmt.where(Candle.ts >= date_from)
    if date_to is not None:
        stmt = stmt.where(Candle.ts <= date_to)
    rows = await db.execute(stmt.order_by(Candle.ts))
    return [
        EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close), volume=float(r.volume))
        for r in rows.scalars()
    ]


def _data_version(candles: list[EngineCandle]) -> str:
    n = len(candles)
    if n == 0:
        return "empty"
    first = candles[0].ts.isoformat()
    last = candles[-1].ts.isoformat()
    total = round(sum(c.close for c in candles), 6)
    raw = f"{n}|{first}|{last}|{total}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_SIG_CACHE: dict = {}
_SIG_CACHE_MAX = 256


def generate_signals(strategy_id: str, params: dict | None, candles: list[EngineCandle]) -> list[dict]:
    # Кэш по (стратегия, параметры, окно данных): drop_useless и основной прогон
    # вызывают одну и ту же генерацию — второй вызов берёт готовое.
    try:
        _key = (strategy_id, json.dumps(params or {}, sort_keys=True), len(candles),
                candles[0].ts if candles else None, candles[-1].ts if candles else None)
    except Exception:
        _key = None
    if _key is not None and _key in _SIG_CACHE:
        return _SIG_CACHE[_key]
    strategy = build_strategy(strategy_id, params)
    warmup = strategy.warmup_bars()
    out: list[dict] = []
    total = len(candles)
    for i in range(1, total + 1):
        # окно фиксированного размера: инкрементальные стратегии видят новый бар
        # по одному разу, оконные используют только хвост — результат тот же,
        # но без O(n^2) копий (зависание на 1min/120д)
        lo = max(0, i - 400)
        sig = strategy.on_bar(CandleWindow(candles, lo, i))
        if sig is None or i <= warmup:
            continue
        out.append(
            {
                "ts": sig.time,
                "side": sig.side.value,
                "status": "CANDIDATE",
                "reason": sig.reason,
                "features": sig.features,
            }
        )
    if _key is not None:
        if len(_SIG_CACHE) >= _SIG_CACHE_MAX:
            _SIG_CACHE.pop(next(iter(_SIG_CACHE)))
        _SIG_CACHE[_key] = out
    return out


async def compute_signals(
    db: AsyncSession,
    figi: str,
    interval_name: str,
    strategy_id: str,
    params: dict | None,
    days: int = DEFAULT_DAYS,
    force: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    candles: list | None = None,
) -> dict:
    card = STRATEGY_CATALOG.get(strategy_id)
    if card is None:
        raise ParamValidationError(f"unknown strategy: {strategy_id}")
    if interval_name not in INTERVAL_NAMES:
        raise ParamValidationError(f"unknown interval: {interval_name}")

    validated = validate_params(strategy_id, params)
    range_key = f"days={days}|{date_from.isoformat() if date_from else ''}|{date_to.isoformat() if date_to else ''}"
    p_hash_raw = canonical_params_hash(validated) + "|" + range_key
    import hashlib as _hl
    p_hash = _hl.sha256(p_hash_raw.encode()).hexdigest()[:16]
    interval_value = int(getattr(INTERVAL_NAMES[interval_name], "value", INTERVAL_NAMES[interval_name]))

    if candles is None:
        await ensure_candles(db, figi, interval_name, days,
                             range_from=date_from, range_to=date_to)
        if date_from is None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            date_from = cutoff if date_from is None else date_from
        candles = await _load_candles(db, figi, interval_value, date_from=date_from, date_to=date_to)
    data_ver = _data_version(candles)

    existing = await db.scalar(
        select(StrategyRun).where(
            StrategyRun.figi == figi,
            StrategyRun.interval_name == interval_name,
            StrategyRun.strategy_id == strategy_id,
            StrategyRun.params_hash == p_hash,
            StrategyRun.engine_version == ENGINE_ID,
        )
    )
    if existing is not None and not force:
        if existing.data_version == data_ver:
            sigs = await load_signals(db, existing.id)
            return {
                "run_id": str(existing.id),
                "cached": True,
                "replaced_stale": False,
                "count": len(sigs),
                "signals": sigs,
            }
        replaced_stale = True
    else:
        replaced_stale = False

    if existing is not None:
        await db.execute(
            delete(StrategyRun).where(
                StrategyRun.id.in_(
                    select(RunDependency.parent_run_id).where(
                        RunDependency.child_run_id == existing.id
                    )
                )
            )
        )
        await db.execute(delete(RunDependency).where(RunDependency.child_run_id == existing.id))
        await db.execute(delete(StrategyRun).where(StrategyRun.id == existing.id))
        await db.flush()

    raw = generate_signals(strategy_id, validated, candles)
    run = StrategyRun(
        figi=figi,
        interval_name=interval_name,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        engine_version=ENGINE_ID,
        data_version=data_ver,
        params=validated,
        params_hash=p_hash,
        bars=len(candles),
        from_ts=candles[0].ts if candles else datetime.now().astimezone(),
        to_ts=candles[-1].ts if candles else datetime.now().astimezone(),
    )
    db.add(run)
    await db.flush()
    for s in raw:
        db.add(StrategySignal(run_id=run.id, figi=figi, **s))
    await db.commit()

    return {
        "run_id": str(run.id),
        "cached": False,
        "replaced_stale": replaced_stale,
        "count": len(raw),
        "signals": [{**s, "ts": s["ts"].isoformat()} for s in raw],
    }


async def load_signals(db: AsyncSession, run_id) -> list[dict]:
    rows = await db.execute(
        select(StrategySignal).where(StrategySignal.run_id == run_id).order_by(StrategySignal.ts)
    )
    return [
        {
            "ts": r.ts.isoformat(),
            "side": r.side,
            "status": r.status,
            "reason": r.reason,
            "features": r.features,
        }
        for r in rows.scalars()
    ]


async def get_run(db: AsyncSession, run_id) -> dict | None:
    run = await db.scalar(select(StrategyRun).where(StrategyRun.id == run_id))
    if run is None:
        return None
    sigs = await load_signals(db, run.id)
    return {
        "run_id": str(run.id),
        "figi": run.figi,
        "interval_name": run.interval_name,
        "strategy_id": run.strategy_id,
        "strategy_version": run.strategy_version,
        "engine_version": run.engine_version,
        "params": run.params,
        "data_version": run.data_version,
        "bars": run.bars,
        "from_ts": run.from_ts.isoformat(),
        "to_ts": run.to_ts.isoformat(),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "count": len(sigs),
        "signals": sigs,
    }


async def list_runs(db: AsyncSession, figi: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(StrategyRun).order_by(desc(StrategyRun.created_at)).limit(limit)
    if figi:
        stmt = stmt.where(StrategyRun.figi == figi)
    rows = await db.execute(stmt)
    return [
        {
            "run_id": str(r.id),
            "figi": r.figi,
            "interval_name": r.interval_name,
            "strategy_id": r.strategy_id,
            "engine_version": r.engine_version,
            "params": r.params,
            "bars": r.bars,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows.scalars()
    ]
