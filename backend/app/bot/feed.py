from __future__ import annotations

import asyncio
import logging
import time as _time
import traceback as _tb
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from t_tech.invest import (
    CandleInstrument,
    CandleInterval,
    SubscriptionInterval,
)
from t_tech.invest import AsyncClient
from t_tech.invest.utils import quotation_to_decimal

from app.database import SessionLocal
from app.services.tinvest import upsert_candles


@dataclass
class ClosedCandle:
    figi: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


INTERVAL_ENUM = {
    "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "10min": CandleInterval.CANDLE_INTERVAL_10_MIN,
    "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
}

SUBSCRIPTION_INTERVAL = {
    "1min": SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
    "5min": SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIVE_MINUTES,
    "10min": SubscriptionInterval.SUBSCRIPTION_INTERVAL_10_MIN,
    "15min": SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES,
}

STEP_SEC = {"1min": 60, "5min": 300, "10min": 600, "15min": 900}

logger = logging.getLogger("candle_feed")


class CandleFeed:
    def __init__(self, token: str, interval_name: str, figis: list[str], target: str | None = None):
        self.token = token
        self.target = target
        self.interval_name = interval_name if interval_name in INTERVAL_ENUM else "5min"
        self.figis = figis
        self.mode = "stream"
        self._stop_requested = False
        self.on_log: object | None = None
        self._persist_buf: list[dict] = []
        # Динамическое добавление/удаление figis «на лету» (карусель бота).
        # Очереди заполняются извне (feed.add_figis / feed.remove_figis),
        # а применяются внутри _stream_grpc / _polling между ответами.
        self._add_q: list[str] = []
        self._rm_q: list[str] = []

    def add_figis(self, figis: list[str]) -> None:
        """Подписаться на новые figis без перезапуска feed (stream + polling).

        figis добавляются в общий список сразу (polling подхватывает по списку),
        а для stream-режима ставится в очередь _add_q — _stream_grpc подпишет.
        """
        for f in figis:
            if not f or f in self.figis:
                continue
            self.figis.append(f)
            self._add_q.append(f)

    def remove_figis(self, figis: list[str]) -> None:
        """Отписаться от figis и убрать их из внутреннего списка feed."""
        for f in figis:
            if f in self.figis:
                self.figis.remove(f)
            if f in self._add_q:
                self._add_q.remove(f)
            if f not in self._rm_q:
                self._rm_q.append(f)

    def _drain_add_q(self) -> list[str]:
        """Достаёт из очереди только те figis, которых ещё нет в stream-подписке."""
        add = [f for f in self._add_q if f in self.figis]
        self._add_q = []
        return add

    def _emit(self, msg: str) -> None:
        # Технические сообщения фида — в debug: в UI (info) не спамим, но пишем в консоль/БД.
        logger.debug("CandleFeed %s", msg)
        cb = self.on_log
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass

    def _queue_persist(self, candle: ClosedCandle) -> None:
        self._persist_buf.append(
            {
                "figi": candle.figi,
                "interval": 1,
                "ts": candle.ts,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": int(candle.volume or 0),
            }
        )

    async def _flush_persist(self) -> None:
        if not self._persist_buf:
            return
        buf, self._persist_buf = self._persist_buf, []
        try:
            async with SessionLocal() as db:
                await upsert_candles(db, buf)
            _info = self._persist_human(buf)
            self._emit(f"записано в БД свечей: {_info}")
        except Exception as e:
            self._emit(f"persist_fail {type(e).__name__}: {str(e)[:120]}")
            self._persist_buf.extend(buf)

    def _persist_human(self, buf: list[dict]) -> str:
        """Человекочитаемая сводка записи: сколько свечей, за какое время и не позно ли."""
        try:
            _d = buf[0]["ts"]
            if not hasattr(_d, "astimezone"):
                _d = datetime.fromisoformat(str(_d).replace("Z", "+00:00"))
            if _d.tzinfo is None:
                _d = _d.replace(tzinfo=timezone.utc)
            _dm = _d.astimezone(ZoneInfo("Europe/Moscow"))
            _lag = (datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")) - _dm).total_seconds()
            if _lag < 30:
                _lag_txt = "свежая"
            elif _lag < 90:
                _lag_txt = f"опоздание ~{int(_lag)} сек"
            else:
                _lag_txt = f"опоздание ~{int(_lag // 60)} мин"
            return f"{len(buf)} шт, первая {buf[0]['figi'][-6:]} за {_dm:%H:%M:%S} МСК · {_lag_txt}"
        except Exception:
            return f"{len(buf)} шт"

    def request_stop(self) -> None:
        self._stop_requested = True

    async def stream(self) -> AsyncIterator[ClosedCandle]:
        self._emit(f"stream start interval={self.interval_name} figis={len(self.figis)} target={self.target or 'prod'}")
        # Первая свеча должна прийти быстро; если стрим «молчит» (не падает,
        # но и не отдаёт данные) — это тоже потеря поставки, уходим в polling.
        # waiting_close() отдаёт бар ТОЛЬКО при закрытии минутной свечи — при
        # старте в середине минуты первый бар придёт через 30-60с. Таймаут
        # 75с (больше максимального ожидания закрытия) держит живой стрим и
        # не даёт упасть в polling с перемоткой исторического баклога.
        first_candle_timeout = 75.0
        # Сколько секунд держим polling-фолбэк между попытками восстановить gRPC.
        poll_recovery_sec = 120.0
        attempt = 0
        while not self._stop_requested:
            attempt += 1
            self.mode = "stream"
            dur = _time.monotonic()
            got_candle = False
            try:
                it = self._stream_grpc().__aiter__()
                while True:
                    item = await asyncio.wait_for(
                        it.__anext__(), timeout=first_candle_timeout
                    )
                    got_candle = True
                    yield item
            except asyncio.CancelledError:
                if self._stop_requested:
                    self._emit("stream cancelled (stop requested)")
                    raise
                tb = _tb.format_exc(limit=4).replace("\n", " | ")[:400]
                self._emit(
                    f"stream cancelled attempt={attempt} got_candle={got_candle} "
                    f"uptime={_time.monotonic() - dur:.1f}s tb=[{tb}]"
                )
                continue
            except StopAsyncIteration:
                self._emit(
                    f"stream finished attempt={attempt} got_candle={got_candle} "
                    f"uptime={_time.monotonic() - dur:.1f}s"
                )
            except asyncio.TimeoutError:
                self._emit(
                    f"stream SILENT (no candle in {first_candle_timeout:.0f}s) "
                    f"attempt={attempt} got_candle={got_candle} "
                    f"uptime={_time.monotonic() - dur:.1f}s"
                )
            except Exception as e:
                self._emit(
                    f"stream gRPC error attempt={attempt} type={type(e).__name__} "
                    f"err={str(e)[:200]} got_candle={got_candle} "
                    f"uptime={_time.monotonic() - dur:.1f}s"
                )
            # gRPC упал/замолчал: какое-то время фолбэк-поллинг, потом снова пробуем gRPC.
            self.mode = "polling"
            self._emit("switching to polling (recovery)")
            try:
                async for item in self._polling(recovery_sec=poll_recovery_sec):
                    yield item
            except asyncio.CancelledError:
                if self._stop_requested:
                    self._emit("polling cancelled (stop requested)")
                    raise
                continue

    async def _stream_grpc(self) -> AsyncIterator[ClosedCandle]:
        interval = SUBSCRIPTION_INTERVAL[self.interval_name]

        # Современный способ (пример easy_async_stream_client.py): client.create_market_data_stream()
        # сам следит за подпиской и при потере стрима переподписывается с экспоненциальным backoff.
        async with AsyncClient(self.token, target=self.target) as client:
            mds = client.create_market_data_stream()
            instruments = [CandleInstrument(figi=f, interval=interval) for f in self.figis]
            mds.candles.waiting_close().subscribe(instruments)
            sub_tries: dict[str, int] = {f: 0 for f in self.figis}
            try:
                async for resp in mds:
                    # Динамическая карусель: применить накопленные подписки/отписки.
                    _add = self._drain_add_q() if self._add_q else []
                    if _add:
                        try:
                            mds.candles.waiting_close().subscribe(
                                [CandleInstrument(figi=f, interval=interval) for f in _add]
                            )
                            self._emit(f"stream subscribe add figis={[f[-6:] for f in _add]}")
                        except Exception as _sa_e:
                            self._emit(f"stream subscribe add failed: {type(_sa_e).__name__} {str(_sa_e)[:120]}")
                    _rm = self._rm_q[:]
                    if _rm:
                        self._rm_q = []
                        try:
                            mds.candles.waiting_close().unsubscribe(
                                [CandleInstrument(figi=f, interval=interval) for f in _rm]
                            )
                            self._emit(f"stream unsubscribe figis={[f[-6:] for f in _rm]}")
                        except Exception as _su_e:
                            self._emit(f"stream unsubscribe failed: {type(_su_e).__name__} {str(_su_e)[:120]}")
                    # Проверка: подтвердилась ли подписка на свечи. Если какой-то figi
                    # не подписан — переподписываемся (но не вечно).
                    sub_resp = getattr(resp, "subscribe_candles_response", None)
                    if sub_resp is not None:
                        for s in sub_resp.candles_subscriptions:
                            figi = s.figi
                            ok = int(s.subscription_status) == 1
                            if ok:
                                if sub_tries.get(figi, 0) > 0:
                                    self._emit(f"stream resubscribed ok figi={figi[-6:]}")
                                sub_tries[figi] = 0
                            else:
                                sub_tries[figi] = sub_tries.get(figi, 0) + 1
                                if sub_tries[figi] > 3:
                                    self._emit(
                                        f"stream subscription FAILED permanently figi={figi[-6:]} "
                                        f"status={int(s.subscription_status)}"
                                    )
                                    continue
                                self._emit(
                                    f"stream subscription retry figi={figi[-6:]} "
                                    f"status={int(s.subscription_status)} try={sub_tries[figi]}"
                                )
                                mds.candles.waiting_close().subscribe(
                                    [CandleInstrument(figi=figi, interval=interval)]
                                )

                    candle = getattr(resp, "candle", None)
                    if candle is None:
                        rtype = type(resp).__name__
                        logger.debug("CandleFeed stream resp type=%s (no candle)", rtype)
                        continue
                    cc = ClosedCandle(
                        figi=candle.figi,
                        ts=candle.time.replace(tzinfo=timezone.utc),
                        open=float(quotation_to_decimal(candle.open)),
                        high=float(quotation_to_decimal(candle.high)),
                        low=float(quotation_to_decimal(candle.low)),
                        close=float(quotation_to_decimal(candle.close)),
                        volume=float(candle.volume),
                    )
                    self._queue_persist(cc)
                    if len(self._persist_buf) >= 10:
                        await self._flush_persist()
                    yield cc
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._emit(f"gRPC disconnect: type={type(e).__name__} err={str(e)[:200]}")
                raise

    async def _polling(self, recovery_sec: float = 0.0) -> AsyncIterator[ClosedCandle]:
        step_sec = STEP_SEC[self.interval_name]
        buffers: dict[str, deque] = {f: deque(maxlen=3) for f in self.figis}
        seen: set[tuple[str, datetime]] = set()
        next_poll = datetime.now(timezone.utc)
        started = _time.monotonic()
        fail_cycles = 0
        async with AsyncClient(self.token, target=self.target) as client:
            while True:
                wait = (next_poll - datetime.now(timezone.utc)).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
                next_poll += timedelta(seconds=step_sec)

                cycle_fail = 0
                for figi in self.figis:
                    try:
                        resp = await client.market_data.get_candles(
                            figi=figi,
                            interval=INTERVAL_ENUM[self.interval_name],
                            from_=datetime.now(timezone.utc) - timedelta(minutes=step_sec * 4),
                            to=datetime.now(timezone.utc),
                        )
                        for c in resp.candles:
                            key = (figi, c.time)
                            if key in seen:
                                continue
                            seen.add(key)
                            if len(seen) > 5000:
                                seen.clear()
                            cc = ClosedCandle(
                                figi=figi,
                                ts=c.time.replace(tzinfo=timezone.utc),
                                open=float(quotation_to_decimal(c.open)),
                                high=float(quotation_to_decimal(c.high)),
                                low=float(quotation_to_decimal(c.low)),
                                close=float(quotation_to_decimal(c.close)),
                                volume=float(c.volume),
                            )
                            self._queue_persist(cc)
                            if len(self._persist_buf) >= 10:
                                await self._flush_persist()
                            yield cc
                    except asyncio.CancelledError:
                        raise
                    except Exception as _e:
                        cycle_fail += 1
                        if cycle_fail == 1:
                            self._emit(
                                f"polling get_candles error {type(_e).__name__} {str(_e)[:100]} "
                                f"(figi={figi[-6:]}) fail_cycles={fail_cycles}"
                            )
                if cycle_fail:
                    fail_cycles += 1
                    if fail_cycles >= 3:
                        self._emit(f"polling FAILED {fail_cycles} циклов подряд — возврат к gRPC")
                        break
                else:
                    fail_cycles = 0
                if recovery_sec and (_time.monotonic() - started) >= recovery_sec:
                    self._emit("polling recovery window expired — пробуем gRPC снова")
                    break
