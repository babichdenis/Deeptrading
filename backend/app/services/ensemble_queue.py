"""Очередь тестов ансамбля (Lab → Ансамбль).

Один прогон = одна конфигурация ансамбля × список акций × период.
Прогресс публикуется в event_bus на канал ensjob:{id} — фронт рисует
прогресс-бары по websocket (JOB_STARTED / FIGI_STARTED / FIGI_COMPLETED /
JOB_PROGRESS / JOB_COMPLETED).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle
from app.models.ensemble_runs import EnsembleRun
from app.models.instrument import Instrument
from app.services.candle_cache import ensure_candles
from app.services.ensemble import compute_ensemble
from app.services.eventbus import event_bus
from app.services.tinvest import INTERVAL_NAMES


async def set_status(run_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionLocal() as db:
        run = await db.get(EnsembleRun, run_id)
        if run is None:
            return
        run.status = status
        if status == "RUNNING" and run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        if status in ("DONE", "FAILED", "CANCELLED"):
            run.finished_at = datetime.now(timezone.utc)
        if error:
            run.error = error
        await db.commit()


async def _save_progress(run_id: uuid.UUID, progress: dict, result: dict | None = None) -> None:
    async with SessionLocal() as db:
        run = await db.get(EnsembleRun, run_id)
        if run is None:
            return
        run.progress = progress
        if result is not None:
            run.result = result
        await db.commit()


async def _execute(run_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        run = await db.get(EnsembleRun, run_id)
        if run is None:
            return
        params = dict(run.params or {})
        figis = list(params.get("figis", []))
        days = int(params.get("days", 30))
        channel = f"ensjob:{run_id}"

    await event_bus.publish(channel, "JOB_STARTED",
                            {"run_id": str(run_id), "status": "RUNNING", "figis": figis})
    total = len(figis)
    by_stock = {f: {"done": 0, "total": 1} for f in figis}
    progress = {"done": 0, "total": total, "current": "", "by_stock": by_stock}
    await _save_progress(run_id, progress)
    results: list[dict] = []
    done = 0
    try:
        interval_value = int(getattr(INTERVAL_NAMES["1min"], "value", INTERVAL_NAMES["1min"]))
        for figi in figis:
            async with SessionLocal() as db:
                inst = await db.scalar(select(Instrument).where(Instrument.figi == figi))
                ticker = inst.ticker if inst else figi
                # НЕ докачиваем с биржи: историю заливаем CSV (load_history_csv.py),
                # а свежие бары уже есть в базе. Биржевой fetch для старых 1m даёт 50002.
                stmt = select(Candle).where(Candle.figi == figi, Candle.interval == interval_value)
                from_ts = params.get("from_ts")
                to_ts = params.get("to_ts")
                if from_ts:
                    stmt = stmt.where(Candle.ts >= datetime.fromisoformat(from_ts.replace("Z", "+00:00")))
                if to_ts:
                    stmt = stmt.where(Candle.ts <= datetime.fromisoformat(to_ts.replace("Z", "+00:00")))
                rows = (await db.execute(stmt.order_by(Candle.ts))).scalars().all()
                candles = [EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high),
                                        low=float(r.low), close=float(r.close), volume=float(r.volume))
                           for r in rows]
            await event_bus.publish(channel, "FIGI_STARTED", {"ticker": ticker})
            body = {k: v for k, v in params.items() if k != "figis"}
            body["figi"] = figi
            res = await asyncio.to_thread(compute_ensemble, candles, body)
            if "error" in res:
                results.append({"ticker": ticker, "figi": figi, "error": res["error"]})
            else:
                e = res["static"]["economic"]
                oc = (res["static"].get("oracle_coverage") or {}).get("causal", {})
                results.append({
                    "ticker": ticker, "figi": figi,
                    "trades": e["trades"], "gross": e["gross"], "costs": e["costs"],
                    "net": e["net"], "pf": e["profit_factor"],
                    "coverage_pct": oc.get("coverage_accepted_pct"),
                })
            done += 1
            progress["done"] = done
            progress["current"] = ticker
            progress["by_stock"][figi]["done"] = 1
            await _save_progress(run_id, progress)
            await event_bus.publish(channel, "FIGI_COMPLETED",
                                    {"ticker": ticker, "done": done, "total": total})
            await event_bus.publish(channel, "JOB_PROGRESS",
                                    {"done": done, "total": total, "current": ticker})

        total_net = sum(r.get("net", 0) for r in results)
        positive = sum(1 for r in results if (r.get("net") or 0) > 0)
        result = {"by_stock": results, "total_net": round(total_net, 2),
                  "positive_stocks": positive, "stocks": len(results)}
        await _save_progress(run_id, progress, result)
        await event_bus.publish(channel, "JOB_COMPLETED",
                                {"run_id": str(run_id), "status": "DONE", "result": result})
        await set_status(run_id, "DONE")
    except asyncio.CancelledError:
        await set_status(run_id, "CANCELLED")
        raise
    except Exception as e:  # noqa: BLE001
        await set_status(run_id, "FAILED", error=str(e)[:400])
        await event_bus.publish(channel, "JOB_FAILED", {"run_id": str(run_id), "error": str(e)[:400]})


class EnsembleQueueDispatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            try:
                async with SessionLocal() as db:
                    row = (await db.execute(
                        select(EnsembleRun).where(EnsembleRun.status == "QUEUED")
                        .order_by(EnsembleRun.created_at).limit(1)
                    )).scalars().first()
                if row is None:
                    await asyncio.sleep(2)
                    continue
                run_id = row.id
                await set_status(run_id, "RUNNING")
                task = asyncio.create_task(_execute(run_id))
                await task
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2)


queue_dispatcher = EnsembleQueueDispatcher()
