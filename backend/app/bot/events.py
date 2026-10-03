from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path


class EventLog:
    """Кольцевой буфер событий бота для UI и аудита (§20 Preview_bot.md).

    Также пишет события в append-only JSONL-файл для переживания рестартов.
    """

    def __init__(self, maxlen: int = 500, persist_path: str | None = None):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._persist_path = persist_path or str(Path(__file__).parent.parent.parent / "data" / "event_log.jsonl")

    def log(self, event_type: str, figi: str | None = None, ticker: str | None = None,
            reason: str | None = None, **payload) -> dict:
        ev = {
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type,
            "figi": figi,
            "ticker": ticker,
            "reason": reason,
            "payload": payload or None,
        }
        request_id = payload.get("request_id")
        if request_id:
            ev["request_id"] = request_id
        self._buf.append(ev)
        self._persist(ev)
        return ev

    def _persist(self, ev: dict) -> None:
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def latest(self, limit: int = 100) -> list[dict]:
        items = list(self._buf)
        return list(reversed(items[-limit:]))

    def __len__(self) -> int:
        return len(self._buf)
