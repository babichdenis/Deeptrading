"""Market data source — только получение данных из T-Invest.

TinvestMarketDataSource не занимается индикаторами, стратегиями, БД.
Он только: подключиться, подписаться, получать, переподключаться,
fallback stream → polling, сообщать connection status.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from app.marketdata.models import Candle

logger = logging.getLogger("candlehub.source")


class MarketDataSource(ABC):
    """Абстракция источника рыночных данных."""

    @abstractmethod
    async def connect(self) -> None:
        """Подключиться к источнику."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Отключиться от источника."""
        ...

    @abstractmethod
    async def subscribe(self, figi: str, timeframe: str) -> None:
        """Подписаться на данные по инструменту и таймфрейму."""
        ...

    @abstractmethod
    async def unsubscribe(self, figi: str, timeframe: str) -> None:
        """Отписаться от данных."""
        ...

    @abstractmethod
    async def stream(self) -> AsyncIterator[Candle]:
        """Поток свечей (async iterator)."""
        ...

    @abstractmethod
    async def fetch_history(
        self,
        figi: str,
        timeframe: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[Candle]:
        """Загрузить историю свечей."""
        ...


class TinvestMarketDataSource(MarketDataSource):
    """Источник данных T-Invest API (stream + polling fallback)."""

    def __init__(self, token: str):
        self._token = token
        self._client = None
        self._connected = False
        self._streaming = False
        self._polling = False
        self._subscriptions: set[tuple[str, str]] = set()

    async def connect(self) -> None:
        """Подключиться к T-Invest API."""
        from t_tech.invest import Client
        self._client = Client(self._token)
        self._connected = True
        logger.info("[MarketData] T-Invest connected")

    async def disconnect(self) -> None:
        """Отключиться от T-Invest API."""
        self._connected = False
        self._streaming = False
        self._polling = False
        self._client = None
        logger.info("[MarketData] T-Invest disconnected")

    async def subscribe(self, figi: str, timeframe: str) -> None:
        """Подписаться на данные по инструменту и таймфрейму."""
        self._subscriptions.add((figi, timeframe))
        logger.info("[MarketData] subscribed %s %s", figi, timeframe)

    async def unsubscribe(self, figi: str, timeframe: str) -> None:
        """Отписаться от данных."""
        self._subscriptions.discard((figi, timeframe))
        logger.info("[MarketData] unsubscribed %s %s", figi, timeframe)

    async def stream(self) -> AsyncIterator[Candle]:
        """Поток свечей (async iterator).

        Fallback: stream → polling.
        """
        if not self._connected:
            await self.connect()

        try:
            async for candle in self._stream_impl():
                yield candle
        except Exception as e:
            logger.warning("[MarketData] stream failed: %s, falling back to polling", e)
            async for candle in self._polling_impl():
                yield candle

    async def _stream_impl(self) -> AsyncIterator[Candle]:
        """Реализация stream (заглушка — нужна реальная реализация)."""
        # TODO: реальная реализация stream через T-Invest API
        self._streaming = True
        if False:  # pragma: no cover
            yield  # type: ignore

    async def _polling_impl(self) -> AsyncIterator[Candle]:
        """Реализация polling fallback."""
        self._polling = True
        while self._polling:
            for figi, timeframe in list(self._subscriptions):
                try:
                    candles = await self.fetch_history(
                        figi, timeframe,
                        datetime.now(timezone.utc) - timedelta(minutes=5),
                        datetime.now(timezone.utc),
                    )
                    for candle in candles:
                        yield candle
                except Exception as e:
                    logger.error("[MarketData] polling failed for %s %s: %s", figi, timeframe, e)
            await asyncio.sleep(60)

    async def fetch_history(
        self,
        figi: str,
        timeframe: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[Candle]:
        """Загрузить историю свечей через T-Invest API."""
        from app.services.tinvest import INTERVAL_NAMES, fetch_candles
        from app.marketdata.models import CandleState

        interval = INTERVAL_NAMES.get(timeframe)
        if interval is None:
            raise ValueError(f"unknown timeframe: {timeframe}")

        rows = await asyncio.to_thread(fetch_candles, figi, interval, date_from, date_to)
        return [
            Candle(
                ts=r["ts"],
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                figi=figi,
                timeframe=timeframe,
                source="history",
                state=CandleState.CLOSED,
            )
            for r in rows
        ]
