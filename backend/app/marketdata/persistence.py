"""CandlePersistence — отдельный consumer для записи свечей в БД.

Persistence не является частью CandleHub. Он подписывается на события
и выполняет batching: 100 свечей → bulk upsert, а не каждая свеча → DB transaction.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.marketdata.models import Candle, CandleEvent

logger = logging.getLogger("candlehub.persistence")


class CandlePersistence:
    """Персистентность свечей с batching.

    Подписывается на CANDLE_CLOSED и накапливает свечи в буфере.
    При достижении batch_size или по таймеру выполняет bulk upsert.
    """

    def __init__(self, db: AsyncSession, batch_size: int = 100, flush_interval_sec: float = 5.0):
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval_sec
        self._buffer: list[Candle] = []
        self._last_flush = datetime.now(timezone.utc)
        self._total_persisted = 0
        self._total_failed = 0

    async def on_candle_closed(self, event: CandleEvent) -> None:
        """Обработчик события CANDLE_CLOSED."""
        self._buffer.append(event.candle)
        if len(self._buffer) >= self._batch_size:
            await self.flush()

    async def flush(self) -> None:
        """Выполнить bulk upsert накопленных свечей."""
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer.clear()

        try:
            from app.services.tinvest import upsert_candles
            from decimal import Decimal

            rows = [
                {
                    "figi": c.figi,
                    "interval": self._tf_to_interval(c.timeframe),
                    "ts": c.ts,
                    "open": Decimal(str(c.open)),
                    "high": Decimal(str(c.high)),
                    "low": Decimal(str(c.low)),
                    "close": Decimal(str(c.close)),
                    "volume": int(c.volume),
                }
                for c in batch
            ]
            await upsert_candles(self._db, rows)
            self._total_persisted += len(batch)
            logger.debug("[Persistence] flushed %d candles (total: %d)", len(batch), self._total_persisted)
        except Exception as e:
            self._total_failed += len(batch)
            logger.error("[Persistence] flush failed: %s (total failed: %d)", e, self._total_failed)

    async def periodic_flush(self) -> None:
        """Периодический flush по таймеру."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    @staticmethod
    def _tf_to_interval(timeframe: str) -> int:
        """Конвертировать таймфрейм в interval value для БД."""
        mapping = {
            "1min": 1, "5min": 5, "10min": 10, "15min": 15,
            "30min": 30, "hour": 60, "2h": 120, "4h": 240,
            "day": 24 * 60, "week": 7 * 24 * 60, "month": 30 * 24 * 60,
        }
        return mapping.get(timeframe, 1)

    @property
    def stats(self) -> dict:
        return {
            "buffered": len(self._buffer),
            "total_persisted": self._total_persisted,
            "total_failed": self._total_failed,
        }
