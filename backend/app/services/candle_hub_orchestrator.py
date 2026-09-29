"""CandleHubOrchestrator — оркестратор поверх CandleHub.

Единая точка входа для всех писателей и читателей свечей:
- seed_from_db: загрузка истории из БД в hub (прогрев)
- ingest_live: передача live-свечи в hub
- fill_gaps: чтение gap_report + докачка через T-Invest
- ensure_ready: комбинация seed + fill_gaps (вызывается при старте)

CandleHub — чистый движок (без сети/БД). Этот модуль — мост между движком
и инфраструктурой (PostgreSQL, T-Invest API, MOEX ISS).

Логирование: все операции логируются с префиксом [CandleHub] для фильтрации.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.candlehub import CandleHub
from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle as DBCandle
from app.services.tinvest import INTERVAL_NAMES, fetch_candles, to_thread, upsert_candles

logger = logging.getLogger("candlehub")

DEFAULT_SEED_DAYS = 120

STANDARD_TFS = ("1min", "5min", "10min", "15min", "30min", "hour", "2h", "4h", "day")


class CandleHubOrchestrator:
    """Оркестратор поверх CandleHub: БД ↔ hub ↔ внешние API."""

    def __init__(self, hub: CandleHub | None = None):
        self.hub = hub or CandleHub()
        logger.info("[CandleHub] orchestrator initialized (hub=%s)", id(self.hub))

    # --- ensure_tfs: предсоздание всех стандартных ТФ ------------------

    def ensure_tfs(self, figi: str) -> None:
        """Предсоздать все стандартные ТФ для figi.

        После вызова ingest_1m автоматически обновляет все производные ряды.
        """
        for tf in STANDARD_TFS:
            self.hub.series(figi, tf)
        logger.info("[CandleHub] ensure_tfs(%s): %d timeframes ready", figi, len(STANDARD_TFS))

    # --- seed: загрузка истории из БД в hub ---------------------------

    async def seed_from_db(
        self,
        db: AsyncSession,
        figi: str,
        days: int = DEFAULT_SEED_DAYS,
    ) -> int:
        """Загрузить 1m-свечи из БД в hub (прогрев рядов).

        Возвращает число загруженных свечей.
        """
        t0 = time.monotonic()
        want_to = datetime.now(timezone.utc)
        want_from = want_to - timedelta(days=days)

        rows = (
            await db.execute(
                select(DBCandle.ts, DBCandle.open, DBCandle.high, DBCandle.low,
                       DBCandle.close, DBCandle.volume)
                .where(
                    DBCandle.figi == figi,
                    DBCandle.interval == 1,
                    DBCandle.ts >= want_from,
                    DBCandle.ts <= want_to,
                )
                .order_by(DBCandle.ts)
            )
        ).all()

        if not rows:
            logger.warning("[CandleHub] seed_from_db(%s): no candles in DB for last %d days", figi, days)
            return 0

        candles = [
            EngineCandle(
                ts=ts,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
            for ts, o, h, l, c, v in rows
        ]
        self.hub.seed_1m(figi, candles, source="db", fire=False)
        elapsed = time.monotonic() - t0
        logger.info("[CandleHub] seed_from_db(%s): %d candles in %.2fs", figi, len(candles), elapsed)
        return len(candles)

    # --- ingest: live-свечи в hub -------------------------------------

    def ingest_live(self, figi: str, candle: EngineCandle) -> str:
        """Передать live-свечу в hub. Возвращает действие (см. CandleHub.ingest_1m).

        Автоматически предсоздаёт все стандартные ТФ при первом вызове.
        """
        if not self.hub.keys():
            self.ensure_tfs(figi)
        action = self.hub.ingest_1m(figi, candle, source="live", fire=True)
        if action in ("replace", "insert"):
            logger.warning("[CandleHub] ingest_live(%s): %s at %s", figi, action, candle.ts)
        return action

    # --- fill_gaps: докачка дыр ----------------------------------------

    async def fill_gaps(
        self,
        db: AsyncSession,
        figi: str,
        *,
        is_trading_minute=None,
    ) -> int:
        """Прочитать gap_report и докачить дыры через T-Invest.

        is_trading_minute: календарь сессий (None = сырой отчёт).
        Возвращает число докачанных свечей.
        """
        t0 = time.monotonic()
        gaps = self.hub.gap_report(figi, is_trading_minute=is_trading_minute)
        if not gaps:
            return 0

        total = 0
        for gap_from, gap_to in gaps:
            interval = INTERVAL_NAMES["1min"]
            rows = await to_thread(fetch_candles, figi, interval, gap_from, gap_to)
            if rows:
                candles = [
                    EngineCandle(
                        ts=r["ts"],
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["volume"]),
                    )
                    for r in rows
                ]
                self.hub.seed_1m(figi, candles, source="rest", fire=False)
                await upsert_candles(db, rows)
                total += len(rows)

        elapsed = time.monotonic() - t0
        if total:
            logger.info("[CandleHub] fill_gaps(%s): downloaded %d candles for %d gaps in %.2fs",
                        figi, total, len(gaps), elapsed)
        else:
            logger.warning("[CandleHub] fill_gaps(%s): %d gaps found but no data downloaded", figi, len(gaps))
        return total

    # --- ensure_ready: комбинация seed + fill_gaps ----------------------

    async def ensure_ready(
        self,
        db: AsyncSession,
        figi: str,
        days: int = DEFAULT_SEED_DAYS,
        *,
        is_trading_minute=None,
    ) -> dict:
        """Прогреть hub из БД и докачить дыры. Вызывается при старте.

        Возвращает статистику операции.
        """
        t0 = time.monotonic()
        seeded = await self.seed_from_db(db, figi, days)
        self.ensure_tfs(figi)
        filled = await self.fill_gaps(db, figi, is_trading_minute=is_trading_minute)
        elapsed = time.monotonic() - t0
        result = {
            "figi": figi,
            "seeded": seeded,
            "filled": filled,
            "hub_keys": self.hub.keys(),
            "elapsed_sec": round(elapsed, 3),
        }
        logger.info("[CandleHub] ensure_ready(%s): %s", figi, result)
        return result

    # --- доступ к данным -----------------------------------------------

    def get_series(self, figi: str, tf: str | int):
        """Получить ряд figi×tf из hub."""
        return self.hub.series(figi, tf)

    def get_snapshot(self, figi: str, tf: str | int, n: int | None = None) -> list[EngineCandle]:
        """Снимок последних n закрытых баров ряда figi×tf."""
        return self.hub.series(figi, tf).snapshot(n)

    def get_stats(self) -> dict:
        """Статистика всех рядов в hub."""
        return self.hub.stats()
