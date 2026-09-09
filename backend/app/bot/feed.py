from __future__ import annotations

import asyncio
import logging
import time as _time
import traceback as _tb
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

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

    def _emit(self, msg: str) -> None:
        logger.info("CandleFeed %s", msg)
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
            self._emit(
                f"persist_ok n={len(buf)} first={buf[0]['figi'][-6:]} ts={buf[0]['ts']}"
            )
        except Exception as e:
            self._emit(f"persist_fail {type(e).__name__}: {str(e)[:120]}")
            self._persist_buf.extend(buf)

    def request_stop(self) -> None:
        self._stop_requested = True

    async def stream(self) -> AsyncIterator[ClosedCandle]:
        t0 = _time.monotonic()
        self._emit(f"stream start interval={self.interval_name} figis={len(self.figis)} target={self.target or 'prod'}")
        # Первая свеча должна прийти быстро; если стрим «молчит» (не падает,
        # но и не отдаёт данные) — это тоже потеря поставки, уходим в polling.
        # waiting_close() отдаёт бар ТОЛЬКО при закрытии минутной свечи — при
        # старте в середине минуты первый бар придёт через 30-60с. Таймаут
        # 75с (больше максимального ожидания закрытия) держит живой стрим и
        # не даёт упасть в polling с перемоткой исторического баклога.
        first_candle_timeout = 75.0
        max_tries = 3
        backoffs = (1.0, 3.0, 10.0)
        for attempt in range(1, max_tries + 1):
            got_candle = False
            dur = _time.monotonic()
            it = None
            try:
                it = self._stream_grpc().__aiter__()
                seen_any = False
                while True:
                    item = await asyncio.wait_for(
                        it.__anext__(), timeout=first_candle_timeout
                    )
                    got_candle = True
                    seen_any = True
                    yield item
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                if self._stop_requested:
                    self._emit("stream cancelled (stop requested)")
                    raise
                tb = _tb.format_exc(limit=4).replace("\n", " | ")[:400]
                self._emit(
                    f"stream cancelled without shutdown attempt={attempt}/{max_tries} "
                    f"got_candle={got_candle} uptime={_time.monotonic() - dur:.1f}s tb=[{tb}]"
                )
                if attempt < max_tries:
                    await asyncio.sleep(backoffs[attempt - 1])
                    continue
            except asyncio.TimeoutError:
                self._emit(
                    f"stream SILENT (no candle in {first_candle_timeout:.0f}s) "
                    f"attempt={attempt}/{max_tries} got_candle={got_candle} "
                    f"uptime={_time.monotonic() - dur:.1f}s"
                )
                if attempt < max_tries:
                    await asyncio.sleep(backoffs[attempt - 1])
                    continue
            except Exception as e:
                self._emit(
                    f"stream gRPC error attempt={attempt}/{max_tries} type={type(e).__name__} "
                    f"err={str(e)[:200]} got_candle={got_candle} uptime={_time.monotonic() - dur:.1f}s"
                )
                if attempt < max_tries:
                    await asyncio.sleep(backoffs[attempt - 1])
                    continue
            break
        self.mode = "polling"
        self._emit("switching to polling (mode=polling)")
        async for item in self._polling():
            yield item

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

    async def _polling(self) -> AsyncIterator[ClosedCandle]:
        step_sec = STEP_SEC[self.interval_name]
        buffers: dict[str, deque] = {f: deque(maxlen=3) for f in self.figis}
        seen: set[tuple[str, datetime]] = set()
        next_poll = datetime.now(timezone.utc)
        async with AsyncClient(self.token, target=self.target) as client:
            while True:
                wait = (next_poll - datetime.now(timezone.utc)).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
                next_poll += timedelta(seconds=step_sec)

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
                    except Exception:
                        continue
