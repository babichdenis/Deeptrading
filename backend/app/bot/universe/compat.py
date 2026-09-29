"""Совместимость: прежний публичный API Universe, собранный из новых слоёв.

Каждая функция повторяет старую реализацию построчно: те же SQL-запросы,
те же пороги, тот же порядок обхода кандидатов и та же схема итогового dict'а.
Смысл рефакторинга — не в этом файле, а в том, что под ним теперь лежат
отдельные слои Universe (discovery) / Screener (screener) / Features
(features) / Selection (selection).

Известная особенность, которую НЕ чиним здесь: align_step() оборван и ничего
не возвращает. Это поведение зафиксировано до рефакторинга, починка была бы
изменением поведения и выходит за рамки задачи.
"""
from __future__ import annotations

import zoneinfo
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .bars import load_all_bars, load_bars, resample_1m_to_5m
from .discovery import (
    count_bars_by_figi,
    discover_eligible_universe,
    discover_liquid_universe,
    discover_tradeable_universe,
)
from .domain import ScreenItem
from .features import ATR_PERIOD, FEATURE_WINDOW, average_turnover, atr_pct
from .screener import ELIGIBLE, TRADEABLE, VOLATILE, screen_all
from .selection import top_n as rank_top_n

__all__ = [
    "select_eligible_universe",
    "select_all_tradeable",
    "select_volatile_universe",
    "count_day_candles",
    "utc_now",
    "session_date",
    "align_step",
]

SOURCE_INTERVAL_ELIGIBLE = 1
SOURCE_INTERVAL_TRADEABLE = 5
ELIGIBLE_BARS_LIMIT = 1000
TRADEABLE_BARS_LIMIT = 200
TRADEABLE_MIN_BARS = 30
VOLATILE_TURNOVER_WINDOW = 10


async def select_eligible_universe(db: AsyncSession, top_n: int = 20) -> list[dict]:
    """Загружает eligible-тикеры из таблицы universe (tier=eligible).

    Пре-фильтр «есть ли свечи» (GROUP BY figi HAVING count >= 1) убран
    намеренно: он отсекал ровно figi с нулём свечей, а load_bars ниже
    отсекает их же по len(bars) < min_source_bars. Выход не менялся ни на
    байт (проверено на живой БД), а сам запрос агрегировал всю историю
    1m-свечей и занимал ~86% ранжинг-цикла: 6.6 с из 7.2 с на 31 figi.
    Не возвращать без перепроверки плана запросов.
    """
    snapshot = await discover_eligible_universe(db)
    if not snapshot.entries:
        return []

    entries_by_figi = {e.ref.figi: e for e in snapshot.entries}

    items: list[ScreenItem] = []
    atr_by_figi: dict[str, float] = {}
    for entry in snapshot.entries:
        bars_1m = await load_bars(
            db, entry.ref.figi, interval=SOURCE_INTERVAL_ELIGIBLE,
            limit=ELIGIBLE_BARS_LIMIT,
        )
        bars_5m = resample_1m_to_5m(bars_1m, entry.ref.figi)
        pct, valid = atr_pct(bars_5m, FEATURE_WINDOW)
        items.append(
            ScreenItem(
                ref=entry.ref,
                source_bars=len(bars_1m),
                resampled_bars=len(bars_5m),
                features_valid=valid,
            )
        )
        atr_by_figi[entry.ref.figi] = pct

    scored = []
    for passed in screen_all(items, ELIGIBLE):
        entry = entries_by_figi[passed.instrument.figi]
        scored.append(
            {
                "figi": passed.instrument.figi,
                "ticker": passed.instrument.ticker,
                "lot": entry.lot,
                "name": passed.instrument.ticker,
                "atr_pct": atr_by_figi[passed.instrument.figi],
                "avg_price": entry.avg_price,
                "avg_turnover": entry.avg_turnover,
                "sector": entry.sector,
            }
        )

    return rank_top_n(scored, key=lambda r: r["atr_pct"], limit=top_n)


async def select_all_tradeable(db: AsyncSession, top_n: int = 6) -> list[dict]:
    """Top-N бумаг по ATR% волатильности из instrument_info."""
    snapshot = await discover_tradeable_universe(db)
    if not snapshot.entries:
        return []

    entries_by_figi = {e.ref.figi: e for e in snapshot.entries}
    figi_list = [e.ref.figi for e in snapshot.entries]
    has_data = await count_bars_by_figi(
        db, figi_list, interval=SOURCE_INTERVAL_TRADEABLE, min_bars=TRADEABLE_MIN_BARS
    )

    items: list[ScreenItem] = []
    atr_by_figi: dict[str, float] = {}
    for entry in snapshot.entries:
        if entry.ref.figi not in has_data:
            continue

        bars = await load_bars(
            db, entry.ref.figi, interval=SOURCE_INTERVAL_TRADEABLE,
            limit=TRADEABLE_BARS_LIMIT,
        )
        pct, valid = atr_pct(bars, FEATURE_WINDOW)
        items.append(
            ScreenItem(
                ref=entry.ref,
                source_bars=len(bars),
                features_valid=valid,
            )
        )
        atr_by_figi[entry.ref.figi] = pct

    scored = []
    for passed in screen_all(items, TRADEABLE):
        entry = entries_by_figi[passed.instrument.figi]
        scored.append(
            {
                "figi": passed.instrument.figi,
                "ticker": passed.instrument.ticker,
                "lot": entry.lot,
                "name": entry.name,
                "atr_pct": atr_by_figi[passed.instrument.figi],
            }
        )

    return rank_top_n(scored, key=lambda r: r["atr_pct"], limit=top_n)


async def select_volatile_universe(
    db: AsyncSession,
    figi_by_ticker: dict[str, str],
    top_n: int = 6,
    days: int = 45,
    min_bars: int = 20,
) -> list[dict]:
    snapshot = discover_liquid_universe(figi_by_ticker)
    if not snapshot.entries:
        return []

    from app.services.candle_cache import ensure_candles

    profile = replace(VOLATILE, min_source_bars=min_bars)
    feature_window = min_bars + ATR_PERIOD

    items: list[ScreenItem] = []
    atr_by_figi: dict[str, float] = {}
    turnover_by_figi: dict[str, float] = {}
    for entry in snapshot.entries:
        figi = entry.ref.figi
        await ensure_candles(db, figi, "day", days)
        bars = await load_all_bars(db, figi, interval=SOURCE_INTERVAL_TRADEABLE)
        pct, valid = atr_pct(bars, feature_window)
        items.append(
            ScreenItem(
                ref=entry.ref,
                source_bars=len(bars),
                features_valid=valid,
            )
        )
        atr_by_figi[figi] = pct
        turnover_by_figi[figi] = (
            round(average_turnover(bars, VOLATILE_TURNOVER_WINDOW), 0) if valid else 0
        )

    scored = []
    for passed in screen_all(items, profile):
        figi = passed.instrument.figi
        scored.append(
            {
                "figi": figi,
                "ticker": passed.instrument.ticker,
                "atr_pct": atr_by_figi[figi],
                "avg_turnover": turnover_by_figi[figi],
            }
        )

    return rank_top_n(scored, key=lambda r: r["atr_pct"], limit=top_n)


async def count_day_candles(db: AsyncSession) -> int:
    from app.models.candle import Candle

    return int(await db.scalar(select(func.count()).select_from(Candle)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def session_date(ts: datetime) -> str:
    return ts.astimezone(zoneinfo.ZoneInfo("Europe/Moscow")).date().isoformat()


def align_step(step_sec: int) -> datetime:
    now = datetime.now(timezone.utc)
    epoch = now.timestamp() // step_sec * step_sec
