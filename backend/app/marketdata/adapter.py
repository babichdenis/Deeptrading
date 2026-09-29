"""Compatibility adapter: старый CandleFeed API → новый CandleHub.

Позволяет постепенно мигрировать на новую архитектуру без полного переписывания.
Старый код продолжает работать через адаптер, который внутри использует CandleHub.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from app.marketdata import Candle, CandleHub, CandleState, TinvestMarketDataSource
from app.marketdata.models import FeedStatus

logger = logging.getLogger("candlehub.adapter")


@dataclass
class ClosedCandle:
    """Совместимый формат старого CandleFeed."""
    figi: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleFeedAdapter:
    """Адаптер старого CandleFeed к новому CandleHub.

    Старый код использует:
        feed = CandleFeed(token, interval, figis)
        async for candle in feed.stream():
            ...

    Новый код использует:
        hub = CandleHub(market_data=TinvestMarketDataSource(token))
        await hub.subscribe(figi, timeframe)
        hub.on(CandleState.CLOSED, handler)

    Адаптер преобразует старый API в новый.
    """

    def __init__(self, token: str, interval_name: str, figis: list[str], target: str | None = None):
        self.token = token
        self.target = target
        self.interval_name = interval_name if interval_name in ("1min", "5min", "10min", "15min") else "5min"
        self.figis = figis
        self.mode = "stream"
        self._stop_requested = False
        self.on_log: Callable | None = None
        self._persist_buf: list[dict] = []

        # Новый hub
        self._hub = CandleHub(market_data=TinvestMarketDataSource(token))
        self._queue: asyncio.Queue[ClosedCandle] = asyncio.Queue()
        self._streaming = False

    def _emit(self, msg: str) -> None:
        logger.info("CandleFeed %s", msg)
        if self.on_log is not None:
            try:
                self.on_log(msg)
            except Exception:
                pass

    async def start(self) -> None:
        """Запустить стрим через новый hub."""
        self._emit(f"stream start interval={self.interval_name} figis={self.figis}")

        for figi in self.figis:
            await self._hub.add_symbol(figi)
            await self._hub.subscribe(figi, self.interval_name)

        # Подписываемся на события CLOSED
        self._hub.on(CandleState.CLOSED, self._on_candle_closed)

        self._streaming = True
        self._emit("stream started")

    def _on_candle_closed(self, event) -> None:
        """Обработчик события CANDLE_CLOSED."""
        candle = event.candle
        closed = ClosedCandle(
            figi=candle.figi,
            ts=candle.ts,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
        try:
            self._queue.put_nowait(closed)
        except asyncio.QueueFull:
            logger.warning("CandleFeedAdapter: queue full, dropping candle")

    async def stop(self) -> None:
        """Остановить стрим."""
        self._stop_requested = True
        self._streaming = False
        self._emit("stream stopped")

    async def stream(self) -> AsyncIterator[ClosedCandle]:
        """Поток закрытых свечей (совместимый API старого CandleFeed)."""
        if not self._streaming:
            await self.start()

        while not self._stop_requested:
            try:
                candle = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield candle
            except asyncio.TimeoutError:
                continue

    def _queue_persist(self, candle: ClosedCandle) -> None:
        """Добавить свечу в буфер персистентности (совместимость со старым API)."""
        # Тот же числовой код, что CandleFeed: INTERVAL_ENUM[interval_name].value.
        from app.bot.feed import INTERVAL_ENUM

        interval = INTERVAL_ENUM[self.interval_name]
        self._persist_buf.append({
            "figi": candle.figi,
            "interval": int(getattr(interval, "value", interval)),
            "ts": candle.ts,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        })

    async def flush_persist(self) -> None:
        """Flush буфера персистентности (совместимость со старым API)."""
        if not self._persist_buf:
            return
        from app.database import SessionLocal
        from app.services.tinvest import upsert_candles

        buf = self._persist_buf[:]
        self._persist_buf.clear()

        async with SessionLocal() as db:
            await upsert_candles(db, buf)
            self._emit(f"persisted {len(buf)} candles")

    @property
    def hub(self) -> CandleHub:
        """Доступ к внутреннему hub (для нового кода)."""
        return self._hub
