"""Signal Trace — эмиттер и JSONL-sink (P0). См. DECISIONS.md 2026-10-01 17:30.

Hot path: emit() собирает документ и кладёт в deque — без IO и await.
Фоновый таск дренирует очередь и пишет JSONL в reports/signal_trace/<run_key>.jsonl
(один файл на прогон — параллельные сессии не смешиваются). seq монотонный,
при переполнении — явный dropped (gap-детект по seq), close() — финальный флаш.

Это первый sink будущего контура (фаза 1: те же события в signal_events).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.engine.trace import SCHEMA_VERSION, RunInfo, TraceEvent, event_id

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "reports" / "signal_trace"


def short_hash(data: Any, *, length: int = 12) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", str(name)).strip("_")[:120] or "run"


class SignalTraceEmitter:
    """Асинхронный JSONL-эмиттер. Один экземпляр на прогон."""

    def __init__(
        self,
        run: RunInfo,
        *,
        directory: Path | str | None = None,
        max_queue: int = 20000,
        flush_every: float = 0.5,
        flush_batch: int = 256,
    ) -> None:
        self.run = run
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self.max_queue = int(max_queue)
        self.flush_every = float(flush_every)
        self.flush_batch = int(flush_batch)
        self.seq = 0
        self.events = 0
        self.errors = 0
        self.dropped = 0
        self._q: deque[dict[str, Any]] = deque()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._path: Path | None = None
        self._fh = None

    # ------------------------------------------------------------------ IO
    @property
    def path(self) -> Path:
        if self._path is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path = self.directory / f"{_safe_name(self.run.run_key)}.jsonl"
        return self._path

    def _file(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        return self._fh

    def _drain(self, max_batch: int) -> int:
        if not self._q:
            return 0
        fh = self._file()
        n = 0
        while self._q and n < max_batch:
            fh.write(json.dumps(self._q.popleft(), ensure_ascii=False) + "\n")
            n += 1
        fh.flush()
        return n

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_every)
            self._drain(self.flush_batch)
            if self._closed and not self._q:
                break

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._flush_loop())

    # --------------------------------------------------------------- emit
    def emit(self, ev: TraceEvent) -> str | None:
        """Синхронная постановка события в очередь (hot path, без IO/await)."""
        try:
            self.seq += 1
            evid = event_id()
            doc = self._render(ev, evid)
            if len(self._q) >= self.max_queue:
                self.dropped += 1
                return evid
            self._q.append(doc)
            self.events += 1
            return evid
        except Exception:
            self.errors += 1
            return None

    def _render(self, ev: TraceEvent, evid: str) -> dict[str, Any]:
        def _ts(value: Any) -> str | None:
            if value is None:
                return None
            return value.isoformat() if hasattr(value, "isoformat") else str(value)

        doc: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "seq": self.seq,
            "event_id": evid,
            "event": ev.stage.value,
            "status": ev.status.value,
            "ts_bar": _ts(ev.ts_bar),
            "ts_wall": datetime.now(timezone.utc).isoformat(),
            "run": self.run.to_dict(),
            "figi": ev.figi or None,
            "ticker": ev.ticker or None,
            "parent_event_id": ev.parent_event_id,
            "reason": ev.reason,
            "reason_code": ev.reason_code,
            "context": ev.context or None,
            "error": ev.error or None,
        }
        if ev.signal_id:
            doc["signal"] = {
                "signal_id": ev.signal_id,
                "side": ev.side,
                "kind": ev.kind,
                "features": ev.features or {},
            }
        if ev.eval_ctx:
            doc["eval"] = ev.eval_ctx
        if ev.action:
            doc["decision"] = {"action": ev.action, "note": ev.reason}
        if ev.order_id:
            doc["order"] = {
                "order_id": ev.order_id,
                "action": ev.action,
                "side": ev.side,
                "qty": ev.qty,
                "price": ev.price,
            }
        return {k: v for k, v in doc.items() if v is not None}

    # -------------------------------------------------------------- close
    def summary(self) -> dict[str, Any]:
        return {
            "run_key": self.run.run_key,
            "events": self.events,
            "dropped": self.dropped,
            "errors": self.errors,
            "max_seq": self.seq,
            "pending": len(self._q),
            "path": str(self.path) if self._path is not None or self.directory else "",
        }

    async def aclose(self) -> dict[str, Any]:
        """Финальный флаш и остановка фонового таска."""
        self._closed = True
        if self._task is not None:
            try:
                await self._task
            except Exception:
                self._task.cancel()
        try:
            self._drain(self.max_queue)
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None
        return self.summary()
