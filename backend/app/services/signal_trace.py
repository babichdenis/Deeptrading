"""Signal Trace — эмиттер, JSONL-sink и DB-sink (P0 + фаза 1). См. DECISIONS.md 2026-10-01 17:30.

Hot path: emit() собирает документ и кладёт в deque — без IO и await.
Фоновый таск дренирует очередь батчами: пишет JSONL в reports/signal_trace/<run_key>.jsonl
(один файл на прогон) и, если задан db_writer, отдаёт тот же батч в БД (SqlTraceWriter,
INSERT ... ON CONFLICT DO NOTHING). seq монотонный, при переполнении — явный dropped,
close() — финальный флаш.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.engine.trace import SCHEMA_VERSION, RunInfo, TraceEvent, event_id

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "reports" / "signal_trace"

DbWriter = Callable[[list[dict[str, Any]]], Awaitable[None]]


def short_hash(data: Any, *, length: int = 12) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", str(name)).strip("_")[:120] or "run"


class SignalTraceEmitter:
    """Асинхронный эмиттер. Один экземпляр на прогон."""

    def __init__(
        self,
        run: RunInfo,
        *,
        directory: Path | str | None = None,
        max_queue: int = 20000,
        flush_every: float = 0.5,
        flush_batch: int = 256,
        db_writer: DbWriter | None = None,
    ) -> None:
        self.run = run
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self.max_queue = int(max_queue)
        self.flush_every = float(flush_every)
        self.flush_batch = int(flush_batch)
        self.db_writer = db_writer
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

    def _take_batch(self, max_batch: int) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while self._q and len(batch) < max_batch:
            batch.append(self._q.popleft())
        return batch

    def _write_file(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        fh = self._file()
        for doc in batch:
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
        fh.flush()

    async def _flush_once(self) -> None:
        batch = self._take_batch(self.flush_batch)
        if not batch:
            return
        self._write_file(batch)
        if self.db_writer is not None:
            try:
                await self.db_writer(batch)
            except Exception:
                self.errors += len(batch)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_every)
            await self._flush_once()
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
            "path": str(self._path or ""),
        }

    async def aclose(self) -> dict[str, Any]:
        """Финальный флаш (файл + БД) и остановка фонового таска."""
        self._closed = True
        if self._task is not None:
            try:
                await self._task
            except Exception:
                self._task.cancel()
        try:
            while self._q:
                await self._flush_once()
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None
        return self.summary()


class SqlTraceWriter:
    """DB-sink фазы 1: run-строка + батч событий (ON CONFLICT DO NOTHING)."""

    def __init__(self, run: RunInfo) -> None:
        self.run = run

    async def start(self) -> None:
        from sqlalchemy import text

        from app.database import SessionLocal
        async with SessionLocal() as db:
            await db.execute(text(
                "INSERT INTO signal_trace_runs (run_id, run_key, contour, mode, feed, test_name, "
                "strategy_id, interval, replay_from, replay_to, config_hash, preset_hash, dataset_version) "
                "VALUES (:run_id, :run_key, :contour, :mode, :feed, :test_name, :strategy_id, :interval, "
                ":replay_from, :replay_to, :config_hash, :preset_hash, :dataset_version) "
                "ON CONFLICT (run_id) DO NOTHING"
            ), self.run.to_dict())
            await db.commit()

    async def write(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        from sqlalchemy import text

        from app.database import SessionLocal
        sql = text(
            "INSERT INTO signal_trace_events (event_id, run_id, seq, ts_bar, ts_wall, figi, ticker, "
            "stage, status, signal_id, parent_event_id, side, kind, reason, reason_code, action, "
            "order_id, price, qty, context, error) VALUES "
            "(:event_id, :run_id, :seq, :ts_bar, :ts_wall, :figi, :ticker, :stage, :status, :signal_id, "
            ":parent_event_id, :side, :kind, :reason, :reason_code, :action, :order_id, :price, :qty, "
            "CAST(:context AS JSON), CAST(:error AS JSON)) ON CONFLICT (event_id) DO NOTHING"
        )
        async with SessionLocal() as db:
            for d in docs:
                sig = d.get("signal") or {}
                dec = d.get("decision") or {}
                order = d.get("order") or {}
                ctx = {
                    "eval": d.get("eval"),
                    "context": d.get("context"),
                    "features": sig.get("features"),
                }
                await db.execute(sql, {
                    "event_id": d["event_id"], "run_id": d["run"]["run_id"], "seq": d["seq"],
                    "ts_bar": d.get("ts_bar"), "ts_wall": d.get("ts_wall"),
                    "figi": d.get("figi") or "", "ticker": d.get("ticker") or "",
                    "stage": d["event"], "status": d["status"],
                    "signal_id": sig.get("signal_id"), "parent_event_id": d.get("parent_event_id"),
                    "side": d.get("side") or sig.get("side") or "",
                    "kind": d.get("kind") or sig.get("kind") or "",
                    "reason": d.get("reason") or "", "reason_code": d.get("reason_code") or "",
                    "action": dec.get("action") or order.get("action") or "",
                    "order_id": order.get("order_id") or "", "price": order.get("price"),
                    "qty": order.get("qty"),
                    "context": json.dumps(ctx, ensure_ascii=False, default=str),
                    "error": json.dumps(d.get("error") or {}, ensure_ascii=False, default=str),
                })
            await db.commit()

    async def close(self, summary: dict[str, Any]) -> None:
        from sqlalchemy import text

        from app.database import SessionLocal
        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE signal_trace_runs SET closed_at = now(), stats = CAST(:stats AS JSON) "
                "WHERE run_id = :run_id"
            ), {"run_id": self.run.run_id,
                "stats": json.dumps(summary, ensure_ascii=False, default=str)})
            await db.commit()
