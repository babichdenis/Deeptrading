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
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
MAX_RING = 2000
MAX_SOURCE = 16  # совпадает с width colums source в bot_logs
MAX_LEVEL = 16
COLLAPSE_WIN = 8.0  # сек: одинаковые подряд записи схлопываются в один тил ×N

# Шум из t_tech: 'uuid GetPortfolio' / 'Letzter trading' — системные трассировки без смысла.
_TECH_NOISE_RE = re.compile(r"^(?:[0-9a-f]{24,64}\s+\S+|None\s+\S+)")
# Индикаторные сообщения, которые дублируются сотни раз и не несут информации в UI.
_NOISE_SOURCES = {"portfolio_reconc"}

_LEGACY_TS_RE = None


def _legacy_re():
    global _LEGACY_TS_RE
    if _LEGACY_TS_RE is None:
        _LEGACY_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")
    return _LEGACY_TS_RE


def msk_now_str() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def strip_legacy_ts(msg: str) -> str:
    """Старые строки в БД хранили '[ts] msg' внутри msg — вырезаем префикс."""
    return _legacy_re().sub("", msg, count=1)


def _is_noise(source: str, msg: str, level: str) -> bool:
    """Есть ли в строке польза для UI. Паразитные трассировки не показываем."""
    if level == "info":
        if source in _NOISE_SOURCES:
            return True
        if source.startswith("t_tech") and _TECH_NOISE_RE.match(msg):
            return True
    return False


@dataclass
class LogRecord:
    id: int
    ts: str          # МСК, "YYYY-MM-DD HH:MM:SS.mmm"
    level: str       # debug/info/warn/error
    source: str      # bot / candle_feed / uvicorn / sandbox_routes / ...
    msg: str
    rep: int = 1     # сколько одинаковых записей схлопнуто
    _w: float = 0.0  # время последнего коллапса (internal)

    @property
    def line(self) -> str:
        return f"[{self.ts}] {self.msg}"

    def to_dict(self) -> dict:
        d = {"id": self.id, "ts": self.ts, "level": self.level,
             "source": self.source, "msg": self.msg}
        if self.rep > 1:
            d["rep"] = self.rep
        return d


class LogHub:
    """Потокобезопасное кольцо структурированных логов для UI.

    Идентичные подряд записи (один source+level+msg в пределах COLLAPSE_WIN)
    схлопываются в одну запись со счётчиком `rep` — UI показывает „×N“ и
    живьём обновляет счётчик.
    """

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
             ts: str | None = None, force: bool = False) -> LogRecord:
        level = (level or "info").lower()[:MAX_LEVEL]
        source = (source or "bot")[:MAX_SOURCE]
        now = time.time()
        with self._lock:
            last = self._ring[-1] if self._ring else None
            if (not force and last is not None
                    and last.source == source and last.level == level
                    and last.msg == msg and (now - last._w) <= COLLAPSE_WIN):
                last.rep += 1
                last._w = now
                return last
            self._seq += 1
            rec = LogRecord(self._seq, ts or msk_now_str(), level, source, msg)
            rec._w = now
            self._ring.append(rec)
            return rec

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

    - uvicorn.access вырезаем СРАЗУ — HTTP-поллинг каждую секунду иначе
      утонул бы в access-строках;
    - паразитные info-трассировки (t_tech uuid+метод, portfolio_reconc)
      фильтруем до попадания в UI.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == "uvicorn.access":
                return
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                msg = f"{msg} -> {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            source = str(record.name)
            level = (record.levelname or "info").lower()
            if _is_noise(source, msg, level):
                return
            hub.push(msg, level=level, source=source)
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