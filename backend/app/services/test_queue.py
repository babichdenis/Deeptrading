from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models.lab_settings import LabSetting
from app.models.test_runs import TestRun
from app.services.eventbus import event_bus
from app.services.warehouse import send_to_lab

logger = logging.getLogger("lab.queue")

DEFAULT_MAX_CONCURRENT = 1


async def get_setting(key: str, default: str) -> str:
    async with SessionLocal() as db:
        row = await db.scalar(select(LabSetting).where(LabSetting.key == key))
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with SessionLocal() as db:
        row = await db.scalar(select(LabSetting).where(LabSetting.key == key))
        if row is None:
            db.add(LabSetting(key=key, value=value))
        else:
            row.value = value
        await db.commit()


async def max_concurrent() -> int:
    raw = await get_setting("max_concurrent_tests", str(DEFAULT_MAX_CONCURRENT))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT


async def enqueue(
    config_id: uuid.UUID,
    tickers: list[str],
    date_from=None,
    date_to=None,
    period_days: int = 30,
) -> TestRun:
    run = TestRun(
        config_id=config_id,
        status="QUEUED",
        tickers=tickers,
        date_from=date_from,
        date_to=date_to,
        period_days=period_days,
        progress={"done": 0, "total": len(tickers) * 2, "current": ""},
    )
    async with SessionLocal() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)
    return run


async def set_status(run_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionLocal() as db:
        run = await db.get(TestRun, run_id)
        if run is None:
            return
        run.status = status
        if error is not None:
            run.error = error
        now = datetime.now(timezone.utc)
        if status == "RUNNING" and run.started_at is None:
            run.started_at = now
        if status in ("DONE", "FAILED", "CANCELLED"):
            run.finished_at = now
        await db.commit()


class QueueDispatcher:
    """Единственный фоновый цикл Lab: запускает тесты из очереди, не больше
    max_concurrent_tests одновременно."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._recover())
            logger.info("QueueDispatcher started")

    async def _recover(self) -> None:
        # после рестарта процесса зависшие RUNNING возвращаем в очередь
        async with SessionLocal() as db:
            runs = (await db.execute(select(TestRun).where(TestRun.status == "RUNNING"))).scalars().all()
            for run in runs:
                run.status = "QUEUED"
                run.error = None
            await db.commit()
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in self._running_tasks:
            t.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("queue tick error: %s", e)
            await asyncio.sleep(1.5)

    async def _tick(self) -> None:
        self._running_tasks = {t for t in self._running_tasks if not t.done()}
        await self._sync_progress()
        limit = await max_concurrent()
        active = len(self._running_tasks)
        if active >= limit:
            return

        # атомарный захват очереди: FOR UPDATE SKIP LOCKED исключает гонку
        async with SessionLocal() as db:
            queued = (
                await db.execute(
                    select(TestRun)
                    .where(TestRun.status == "QUEUED")
                    .order_by(TestRun.created_at)
                    .limit(limit - active)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            now = datetime.now(timezone.utc)
            for run in queued:
                run.status = "RUNNING"
                run.started_at = now
            await db.commit()

        for run in queued:
            logger.warning("TICK: запускаю run %s", str(run.id)[:8])
            await event_bus.publish(
                f"job:{run.id}", "JOB_STARTED",
                {"run_id": str(run.id), "config_id": str(run.config_id), "status": "RUNNING"},
                entity_type="test_run", entity_id=str(run.id),
            )
            task = asyncio.create_task(self._execute(run.id))
            self._running_tasks.add(task)

    async def _sync_progress(self) -> None:
        from app.models.configurations import Configuration

        async with SessionLocal() as db:
            runs = (await db.execute(select(TestRun).where(TestRun.status == "RUNNING"))).scalars().all()
            for run in runs:
                cfg = await db.get(Configuration, run.config_id)
                if cfg and cfg.lab_result:
                    p = cfg.lab_result.get("progress")
                    if p:
                        run.progress = p
            await db.commit()

    async def _execute(self, run_id: uuid.UUID) -> None:
        async with SessionLocal() as db:
            run = await db.get(TestRun, run_id)
            if run is None:
                return
            config_id = run.config_id
            tickers = list(run.tickers)
            date_from = run.date_from
            date_to = run.date_to
            period_days = run.period_days

        logger.warning("RUN %s: start config=%s tickers=%s period=%s", str(run_id)[:8], config_id, tickers, period_days)
        try:
            async with SessionLocal() as db2:
                await send_to_lab(
                    db2,
                    config_id,
                    period_days=period_days,
                    top_n=len(tickers) or 10,
                    tickers=tickers,
                    date_from=date_from,
                    date_to=date_to,
                    channel=f"job:{run_id}",
                )
            logger.warning("RUN %s: send_to_lab completed", str(run_id)[:8])
            await event_bus.publish(
                f"job:{run_id}", "JOB_COMPLETED",
                {"run_id": str(run_id), "config_id": str(config_id), "status": "DONE"},
                entity_type="test_run", entity_id=str(run_id),
            )
            await set_status(run_id, "DONE")
        except asyncio.CancelledError:
            logger.warning("RUN %s: cancelled -> QUEUED", str(run_id)[:8])
            await set_status(run_id, "QUEUED")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("RUN %s: failed: %s", str(run_id)[:8], str(e)[:200])
            await set_status(run_id, "FAILED", error=str(e)[:400])


queue_dispatcher = QueueDispatcher()
