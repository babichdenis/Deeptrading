from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path


class EventLog:
    """Кольцевой буфер событий бота для UI и аудита (§20 Preview_bot.md).

    Также пишет события в durable outbox (таблица event_log) для переживания рестартов.
    """

    def __init__(self, maxlen: int = 500, persist_path: str | None = None):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._persist_path = persist_path or str(Path(__file__).parent.parent.parent / "data" / "event_log.jsonl")
        self._outbox: asyncio.Queue | None = None

    def log(self, event_type: str, figi: str | None = None, ticker: str | None = None,
            reason: str | None = None, **payload) -> dict:
        from app.bot.runtime import _request_id_ctx
        ev = {
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type,
            "figi": figi,
            "ticker": ticker,
            "reason": reason,
            "payload": payload or None,
        }
        request_id = payload.get("request_id") or _request_id_ctx.get()
        if request_id:
            ev["request_id"] = request_id
        self._buf.append(ev)
        self._persist(ev)
        return ev

    def _persist(self, ev: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            from app.services.eventbus import event_bus
            task = loop.create_task(event_bus.publish(
                channel="bot_events",
                event_type=ev["type"],
                payload=ev,
                entity_type="bot",
                entity_id=ev.get("figi", ""),
            ))
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        except Exception:
            pass

    def latest(self, limit: int = 100) -> list[dict]:
        items = list(self._buf)
        return list(reversed(items[-limit:]))

    def __len__(self) -> int:
        return len(self._buf)
