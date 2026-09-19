"""Единый контур логирования live-бота.

LogHub — центральное in-memory кольцо структурированных записей
(level/source/ts/msg) для UI. В него пишут:
  - Runtime._log()  — все пользовательские сообщения бота;
  - HubHandler      — подключён к корневому logging и затаскивает в UI
                      ВСЕ консольные записи процесса (feed, stream_manager,
                      sandbox_routes, uvicorn.error и т.д.), т.е. лог больше
                      не расходится на «свои строчки» и «то, что только в
                      консоли».

Персист в таблицу bot_logs (PostgreSQL) выполняет runtime._flush_persist.
Историю за границей кольца читает GET /api/v1/bot/logs/history напрямую из БД.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
MAX_RING = 2000
MAX_SOURCE = 16  # совпадает с width colums source в bot_logs
MAX_LEVEL = 16

_LEGACY_TS_RE = None


def _legacy_re():
    global _LEGACY_TS_RE
    if _LEGACY_TS_RE is None:
        import re
        _LEGACY_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")
    return _LEGACY_TS_RE


def msk_now_str() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def strip_legacy_ts(msg: str) -> str:
    """Старые строки в БД хранили '[ts] msg' внутри msg — вырезаем префикс."""
    return _legacy_re().sub("", msg, count=1)


@dataclass
class LogRecord:
    id: int
    ts: str          # МСК, "YYYY-MM-DD HH:MM:SS.mmm"
    level: str       # debug/info/warn/error
    source: str      # bot / candle_feed / uvicorn / sandbox_routes / ...
    msg: str

    @property
    def line(self) -> str:
        return f"[{self.ts}] {self.msg}"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "level": self.level,
                "source": self.source, "msg": self.msg}


class LogHub:
    """Потокобезопасное кольцо структурированных логов для UI."""

    def __init__(self, maxlen: int = MAX_RING) -> None:
        self._ring: deque[LogRecord] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def maxlen(self) -> int:
        return self._ring.maxlen

    def set_seq(self, n: int) -> None:
        """Сидирование счётчика после восстановления хвоста из bot_logs,
        чтобы инкрементальный `after_id` фронта не ломался после рестарта."""
        with self._lock:
            self._seq = max(self._seq, int(n or 0))

    def push(self, msg: str, level: str = "info", source: str = "bot",
             ts: str | None = None) -> LogRecord:
        rec = LogRecord(
            id=self._next_id(),
            ts=ts or msk_now_str(),
            level=(level or "info").lower()[:MAX_LEVEL],
            source=(source or "bot")[:MAX_SOURCE],
            msg=msg,
        )
        with self._lock:
            self._ring.append(rec)
        return rec

    def _next_id(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def len(self) -> int:
        with self._lock:
            return len(self._ring)

    def tail(self, limit: int = 400) -> list[LogRecord]:
        with self._lock:
            items = list(self._ring)
        return items[-limit:]

    def after(self, after_id: int, limit: int = 400) -> list[LogRecord]:
        with self._lock:
            items = [r for r in self._ring if r.id > after_id]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()


hub = LogHub()

_HANDLER_ATTACHED = False


class HubHandler(logging.Handler):
    """Затаскивает стандартные logging-записи процесса в LogHub (единый контур).

    uvicorn.access вырезаем СРАЗУ — HTTP-поллинг каждую секунду иначе
    утонул бы в access-строках.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == "uvicorn.access":
                return
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                msg = f"{msg} -> {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            hub.push(msg, level=record.levelname.lower(), source=str(record.name))
        except Exception:
            pass


def attach_hub_handler() -> bool:
    """Идемпотентно вешает HubHandler на корневой логгер."""
    global _HANDLER_ATTACHED
    if _HANDLER_ATTACHED:
        return True
    try:
        h = HubHandler()
        h.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(h)
        _HANDLER_ATTACHED = True
        return True
    except Exception:
        return False