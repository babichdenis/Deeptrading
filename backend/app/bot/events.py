from __future__ import annotations

from collections import deque
from datetime import datetime, timezone


class EventLog:
    """Кольцевой буфер событий бота для UI и аудита (§20 Preview_bot.md)."""

    def __init__(self, maxlen: int = 500):
        self._buf: deque[dict] = deque(maxlen=maxlen)

    def log(self, event_type: str, figi: str | None = None, ticker: str | None = None,
            reason: str | None = None, **payload) -> dict:
        ev = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "figi": figi,
            "ticker": ticker,
            "reason": reason,
            "payload": payload or None,
        }
        self._buf.append(ev)
        return ev

    def latest(self, limit: int = 100) -> list[dict]:
        items = list(self._buf)
        return list(reversed(items[-limit:]))

    def __len__(self) -> int:
        return len(self._buf)
