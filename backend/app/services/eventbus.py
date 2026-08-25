from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sequence: Mapped[int] = mapped_column(BigInteger, index=True)
    channel: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered: Mapped[bool] = mapped_column(default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventBus:
    """In-process pub/sub поверх устойчивого event_log (outbox).

    Событие: записать в БД (sequence) → разослать подписчикам → пометить delivered.
    При reconnect фронт шлёт RESUME с last_sequence — отдаём пропущенное из БД.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._sequence = 0

    async def _next_sequence(self) -> int:
        from sqlalchemy import func, select

        async with SessionLocal() as db:
            val = await db.scalar(select(func.max(EventLog.sequence)))
            return int(val or 0) + 1

    async def publish(
        self,
        channel: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        entity_type: str = "",
        entity_id: str = "",
    ) -> dict:
        seq = await self._next_sequence()
        event = {
            "event_id": uuid.uuid4().hex[:12],
            "event_type": event_type,
            "event_version": 1,
            "sequence": seq,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "source": "backend",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "channel": channel,
            "payload": payload or {},
        }
        async with SessionLocal() as db:
            db.add(
                EventLog(
                    sequence=seq,
                    channel=channel,
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload or {},
                    delivered=False,
                    occurred_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        await self._fanout(channel, event)
        return event

    async def _fanout(self, channel: str, event: dict) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(channel, set()))
        dead = []
        for q in queues:
            try:
                q.put_nowait(json.dumps(event, ensure_ascii=False))
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            async with self._lock:
                for q in dead:
                    self._subscribers.get(channel, set()).discard(q)

    async def subscribe(self, channel: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.setdefault(channel, set()).add(queue)

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.get(channel, set()).discard(queue)

    async def events_after(self, channel: str, last_sequence: int, limit: int = 200) -> list[dict]:
        from sqlalchemy import select

        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(EventLog)
                    .where(EventLog.channel == channel, EventLog.sequence > last_sequence)
                    .order_by(EventLog.sequence)
                    .limit(limit)
                )
            ).scalars().all()
        return [
            {
                "event_id": f"evt_{r.id}",
                "event_type": r.event_type,
                "event_version": 1,
                "sequence": r.sequence,
                "occurred_at": r.occurred_at.isoformat(),
                "source": "backend",
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "channel": r.channel,
                "payload": r.payload or {},
            }
            for r in rows
        ]


event_bus = EventBus()
