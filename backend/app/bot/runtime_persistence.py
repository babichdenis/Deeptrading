from __future__ import annotations

import asyncio
import json
import time as _time
from collections import deque
from datetime import timedelta

from app.bot.runtime_diagnostics import audit_swallow as _audit_swallow


class RuntimePersistenceMixin:
    @staticmethod
    def _dq_overflow(q: deque, n_items: int) -> int:
        """P0.3: сколько записей вытеснит maxlen deque при добавлении n_items."""
        return max(0, len(q) + n_items - q.maxlen) if q.maxlen else 0


    async def _flush_persist_once(self) -> None:
        from sqlalchemy import text as _text

        def _drain(q: deque) -> list:
            return [q.popleft() for _ in range(len(q))]

        # P0.3: глубина очередей persist — видна в /bot/status (metrics).
        self.metrics["persist_queue_depth"] = (
            len(self._persist_queue) + len(self._persist_queue_5m) + len(self._log_persist_queue)
        )

        batch = _drain(self._persist_queue)
        batch5 = _drain(self._persist_queue_5m)
        log_rows = _drain(self._log_persist_queue)

        candle_ok = False
        if batch or batch5:
            try:
                _tp0 = _time.perf_counter()
                sql = _text(
                    "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
                    "VALUES (:f, 1, :ts, :o, :h, :l, :c, :v) "
                    "ON CONFLICT (figi, interval, ts) DO UPDATE SET "
                    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    "close=EXCLUDED.close, volume=EXCLUDED.volume"
                )
                sql5 = _text(
                    "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
                    "VALUES (:f, 5, :ts, :o, :h, :l, :c, :v) "
                    "ON CONFLICT (figi, interval, ts) DO UPDATE SET "
                    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    "close=EXCLUDED.close, volume=EXCLUDED.volume"
                )
                async with self._database_session() as db:
                    for statement, rows in ((sql, batch), (sql5, batch5)):
                        if rows:
                            await db.execute(statement, [
                                {"f": f, "ts": ts, "o": o, "h": h, "l": l, "c": cl, "v": v}
                                for f, ts, o, h, l, cl, v in rows
                            ])
                    await db.commit()
                    try:
                        from app.services.candle_integrity import invalidate as _inv
                        for _batch, _iv in ((batch, 1), (batch5, 5)):
                            _figs = sorted({x[0] for x in _batch})
                            _days = sorted({(x[1] + timedelta(hours=3)).date() for x in _batch})
                            if _figs and _days:
                                await _inv(db, _figs, _iv, _days)
                        await db.commit()
                    except Exception as exc:
                        _audit_swallow("persist.integrity_invalidation", exc)
                _pt = (_time.perf_counter() - _tp0) * 1000
                self.metrics["persist_ms_total"] += _pt
                self.metrics["persist_ms_n"] += 1
                if _pt > self.metrics["persist_ms_max"]:
                    self.metrics["persist_ms_max"] = _pt
                self._persist_flushes += 1
                _s = (batch[0] if batch else batch5[0])
                self._log(
                    f"TECHINFO FLUSH_ITEM f={_s[0][-6:]} ts={_s[1]} "
                    f"iv={1 if batch else 5} flushes={self._persist_flushes} "
                    f"q={len(batch)} q5={len(batch5)}",
                    level="debug",
                )
                if self._persist_flushes % 10 == 0:
                    self._log(
                        f"TECHINFO persist ok q={len(batch)} q5={len(batch5)} "
                        f"flushes={self._persist_flushes}",
                        level="debug",
                    )
                candle_ok = True
                if getattr(self, "_persist_candles_degraded", False):
                    self._log("PERSIST_RECOVERED candle writes resumed", source="persist")
                self._persist_candles_degraded = False
            except Exception as e:
                # P0.3: requeue возвращает батч, но при переполнении maxlen deque
                # тихо вытесняет старейшие записи — считаем и то, и другое.
                self.metrics["persist_dropped"] += (
                    self._dq_overflow(self._persist_queue, len(batch))
                    + self._dq_overflow(self._persist_queue_5m, len(batch5))
                )
                self.metrics["persist_failures"] += 1
                _audit_swallow("persist.candles", e)
                if not getattr(self, "_persist_candles_degraded", False):
                    self._log(f"PERSIST_ERR {type(e).__name__}: candle batch requeued", level="error", source="persist")
                self._persist_candles_degraded = True
                self._persist_queue.extend(batch)
                self._persist_queue_5m.extend(batch5)
        else:
            candle_ok = True

        if log_rows and candle_ok:
            try:
                async with self._database_session() as db:
                    _payload = [{"level": l, "source": s, "msg": m, "ts": t + "+03:00", "request_id": r or None}
                                for (l, s, m, t, r) in log_rows]
                    try:
                        await db.execute(
                            _text(
                                "INSERT INTO bot_logs (level, source, ts, msg, request_id) "
                                "SELECT * FROM jsonb_to_recordset(:rows) AS t(level text, source text, ts timestamptz, msg text, request_id text)"
                            ),
                            {"rows": json.dumps(_payload, ensure_ascii=False)},
                        )
                    except Exception as exc:
                        _audit_swallow("persist.logs_request_id_fallback", exc)
                        await db.rollback()
                        _payload = [{"level": l, "source": s[:64], "msg": m, "ts": t + "+03:00"}
                                    for (l, s, m, t, r) in log_rows]
                        await db.execute(
                            _text(
                                "INSERT INTO bot_logs (level, source, ts, msg) "
                                "SELECT * FROM jsonb_to_recordset(:rows) AS t(level text, source text, ts timestamptz, msg text)"
                            ),
                            {"rows": json.dumps(_payload, ensure_ascii=False)},
                        )
                    await db.commit()
                if getattr(self, "_persist_logs_degraded", False):
                    self._log("PERSIST_RECOVERED log writes resumed", source="persist")
                self._persist_logs_degraded = False
            except Exception as e:
                self.metrics["persist_dropped"] += self._dq_overflow(self._log_persist_queue, len(log_rows))
                self.metrics["persist_failures"] += 1
                _audit_swallow("persist.logs", e)
                if not getattr(self, "_persist_logs_degraded", False):
                    self._log(f"PERSIST_LOG_ERR {type(e).__name__}: log batch requeued", level="error", source="persist")
                self._persist_logs_degraded = True
                self._log_persist_queue.extendleft(reversed(log_rows))
        elif log_rows:
            self._log_persist_queue.extendleft(reversed(log_rows))


    async def _flush_persist(self) -> None:
        while self.running:
            await asyncio.sleep(3.0)
            if (not self._persist_queue and not self._persist_queue_5m
                    and not self._log_persist_queue):
                continue
            await self._flush_persist_once()
