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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.candlehub import CandleHub
from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle as DBCandle
from app.services import candle_ingestion_contract as cic
from app.services.tinvest import INTERVAL_NAMES, fetch_candles, to_thread, upsert_candles

logger = logging.getLogger("candlehub")

DEFAULT_SEED_DAYS = 120

STANDARD_TFS = ("1min", "5min", "10min", "15min", "30min", "hour", "2h", "4h", "day")


@dataclass
class FillGapsResult:
    """Структурированный итог fill_gaps (B1, CODE-FIRST-R1).

    Различает fetched (получено от источника) / committed (записано в БД) /
    remaining gaps (источник вернул пусто — НЕ доказанное no-trade) / source errors.
    Пустой успешный ответ источника — не ошибка и не готовность.
    """

    figi: str
    gaps_total: int = 0
    fetched: int = 0
    committed: int = 0
    remaining_gaps: list[tuple[str, str]] = field(default_factory=list)
    source_errors: list[dict] = field(default_factory=list)
    hub_publish_errors: list[dict] = field(default_factory=list)
    scope_rejections: list[dict] = field(default_factory=list)  # строки не принадлежат запросу (figi/interval/окно)

    @property
    def has_source_errors(self) -> bool:
        return bool(self.source_errors)

    @property
    def fully_filled(self) -> bool:
        return (not self.remaining_gaps and not self.source_errors
                and not self.scope_rejections)


class CandleHubOrchestrator:
    """Оркестратор поверх CandleHub: БД ↔ hub ↔ внешние API."""

    EMPTY_GAP_TTL_S = 3600.0  # конечный TTL кэша пустых дыр (не бессрочный)
    RECOVER_HUB_DAYS = 3      # окно восстановления hub из БД после провала публикации

    def __init__(self, hub: CandleHub | None = None, *, clock=None):
        self.hub = hub or CandleHub()
        # Pending recovery по FIGI: figi -> список точных committed диапазонов
        # (dfrom, dto) неуспешной публикации. Не общий bool — независимость инструментов.
        self._pending_recovery: dict[str, list[tuple[datetime, datetime]]] = {}
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

    async def _recover_hub_from_db(self, db: AsyncSession, figi: str, *,
                                  dfrom: datetime | None = None,
                                  dto: datetime | None = None) -> int:
        """Восстановить hub из БД за диапазон [dfrom, dto] (committed данные).

        Используется после провала публикации в hub: данные уже в БД, и hub должен
        быть синхронизирован с ними, даже если источник сейчас недоступен.
        Без диапазона — последние RECOVER_HUB_DAYS дней.
        """
        if dto is None:
            dto = datetime.now(timezone.utc)
        if dfrom is None:
            dfrom = dto - timedelta(days=self.RECOVER_HUB_DAYS)
        rows = (await db.execute(
            select(DBCandle.ts, DBCandle.open, DBCandle.high, DBCandle.low,
                   DBCandle.close, DBCandle.volume)
            .where(DBCandle.figi == figi, DBCandle.interval == 1,
                   DBCandle.ts >= dfrom, DBCandle.ts <= dto)
            .order_by(DBCandle.ts)
        )).all()
        if not rows:
            return 0
        candles = [EngineCandle(ts=ts, open=float(o), high=float(h), low=float(lo),
                                close=float(c), volume=float(v))
                   for ts, o, h, lo, c, v in rows]
        self.hub.seed_1m(figi, candles, source="db", fire=False)
        logger.info("[CandleHub] recover_hub_from_db(%s): %d candles from DB", figi, len(candles))
        return len(candles)

    async def fill_gaps(
        self,
        db: AsyncSession,
        figi: str,
        *,
        is_trading_minute=None,
    ) -> int:
        """Докачать дыры; возвращает число ЗАПИСАННЫХ в БД свечей (int-контракт сохранён)."""
        res = await self.fill_gaps_detailed(db, figi, is_trading_minute=is_trading_minute)
        return res.committed

    async def fill_gaps_detailed(
        self,
        db: AsyncSession,
        figi: str,
        *,
        is_trading_minute=None,
    ) -> FillGapsResult:
        """Докачка дыр. Атомарность: сначала commit в БД, потом публикация в hub;
        провал записи прерывает до hub (память не опережает БД). Пустой ответ
        источника — remaining gap (не ошибка, не доказанное no-trade). Кэш пустых
        дыр — с конечным TTL (восстановление по истечении). Проверка принадлежности
        запросу (figi/interval/окно) — до записи/публикации."""
        t0 = time.monotonic()
        # Восстановление hub из БД для данного FIGI по точным диапазонам неуспешной
        # публикации (committed данные). Очищаем только успешно восстановленные;
        # ошибка восстановления сохраняет pending.
        pending = self._pending_recovery.get(figi, [])
        if pending:
            still_pending: list[tuple[datetime, datetime]] = []
            for dfrom, dto in pending:
                try:
                    await self._recover_hub_from_db(db, figi, dfrom=dfrom, dto=dto)
                except Exception:
                    still_pending.append((dfrom, dto))
            if still_pending:
                self._pending_recovery[figi] = still_pending
            else:
                self._pending_recovery.pop(figi, None)

        gaps = self.hub.gap_report(figi, is_trading_minute=is_trading_minute)
        result = FillGapsResult(figi=figi, gaps_total=len(gaps))
        if not gaps:
            return result

        interval = INTERVAL_NAMES["1min"]
        # Дедуп пустых дыр — ТОЛЬКО внутри этой операции (между вызовами кэш пуст:
        # поздно пришедшая свеча на том же экземпляре оркестратора обязана докачаться).
        empty_cache: set[tuple[str, datetime, datetime]] = set()
        for gap_from, gap_to in gaps:
            if (figi, gap_from, gap_to) in empty_cache:
                result.remaining_gaps.append((gap_from.isoformat(), gap_to.isoformat()))
                continue
            try:
                rows = await to_thread(fetch_candles, figi, interval, gap_from, gap_to)
            except Exception as exc:
                result.source_errors.append({
                    "gap": (gap_from.isoformat(), gap_to.isoformat()),
                    "error": type(exc).__name__,
                })
                continue
            if not rows:
                # пустой успешный ответ != доказанное no-trade; дедуп внутри операции
                empty_cache.add((figi, gap_from, gap_to))
                result.remaining_gaps.append((gap_from.isoformat(), gap_to.isoformat()))
                continue
            # Проверка принадлежности запросу (figi/interval/окно) ДО записи/публикации;
            # отказ — структурированный (без записи и без публикации), не исключение.
            try:
                cic.assert_request_scope(rows, figi=figi, interval=1, window=(gap_from, gap_to))
            except cic.CandleValidationError as exc:
                result.scope_rejections.append({
                    "gap": (gap_from.isoformat(), gap_to.isoformat()),
                    "reason": exc.rejections[0].reason if exc.rejections else "scope_mismatch",
                })
                continue
            result.fetched += len(rows)
            # DB СНАЧАЛА (атомарно, ошибки пропагируют); hub — только после успешного commit.
            committed = await upsert_candles(db, rows)
            result.committed += committed
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
            try:
                self.hub.seed_1m(figi, candles, source="rest", fire=False)
            except Exception as exc:
                # данные уже в БД (committed): recoverable статус + восстановление из БД при повторе
                result.hub_publish_errors.append({
                    "gap": (gap_from.isoformat(), gap_to.isoformat()),
                    "error": type(exc).__name__,
                })
                # точный committed диапазон неуспешной публикации → pending recovery (по FIGI)
                ts_min = min(cic.utc_ts(r["ts"]) for r in rows)
                ts_max = max(cic.utc_ts(r["ts"]) for r in rows)
                pend = self._pending_recovery.setdefault(figi, [])
                if (ts_min, ts_max) not in pend:
                    pend.append((ts_min, ts_max))
            # частичное заполнение: минуты окна, которых источник не вернул, остаются gap
            covered = {cic.utc_ts(r["ts"]) for r in rows}
            m = gap_from
            uncovered: list[datetime] = []
            while m <= gap_to:
                if (is_trading_minute is None or is_trading_minute(m)) and m not in covered:
                    uncovered.append(m)
                m += timedelta(minutes=1)
            if uncovered:
                result.remaining_gaps.append((uncovered[0].isoformat(), uncovered[-1].isoformat()))

        elapsed = time.monotonic() - t0
        if result.committed:
            logger.info("[CandleHub] fill_gaps(%s): committed %d candles for %d/%d gaps in %.2fs",
                        figi, result.committed, result.gaps_total - len(result.remaining_gaps)
                        - len(result.source_errors), result.gaps_total, elapsed)
        else:
            logger.warning(
                "[CandleHub] fill_gaps(%s): %d gaps; committed=0 remaining=%d source_errors=%d in %.2fs",
                figi, result.gaps_total, len(result.remaining_gaps), len(result.source_errors), elapsed)
        return result

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
        fill = await self.fill_gaps_detailed(db, figi, is_trading_minute=is_trading_minute)
        elapsed = time.monotonic() - t0
        result = {
            "figi": figi,
            "seeded": seeded,
            "filled": fill.committed,
            "fill_fetched": fill.fetched,
            "fill_committed": fill.committed,
            "fill_remaining_gaps": fill.remaining_gaps,
            "fill_source_errors": fill.source_errors,
            "fill_hub_publish_errors": fill.hub_publish_errors,
            "fill_fully_filled": fill.fully_filled,
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
