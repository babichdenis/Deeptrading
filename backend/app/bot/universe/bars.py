"""Доступность и загрузка баров, ресемпл 1m → 5m.

Ресемплинг идёт через канонический app.marketdata.resampler.Resampler
(тот же агрегатор, что у ReplayFeed), а не через локальную копию: это
гарантирует, что ATR в Universe считается по тем же бакетам, что и движок.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feed import ClosedCandle
from app.engine.models import Candle as EngineCandle
from app.marketdata.resampler import Resampler
from app.models.candle import Candle

RESAMPLE_TARGET = "5min"
RESAMPLE_PERIOD_MIN = 5


def to_engine_candle(row) -> EngineCandle:
    return EngineCandle(
        ts=row.ts,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=float(row.volume),
    )


def resample_1m_to_5m(bars_1m: list, figi: str) -> list[EngineCandle]:
    """Агрегирует минутные бары в 5m. bars_1m должен быть по возрастанию ts.

    ts закрытого бара = начало бакета (конвенция T-Invest), последний
    неполный бакет включается — как и в legacy-инлайне.
    """
    resampler = Resampler(RESAMPLE_TARGET)
    out: list[EngineCandle] = []
    for b in bars_1m:
        bar = ClosedCandle(
            figi=figi,
            ts=b.ts,
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume),
        )
        emitted = resampler.feed(bar)
        if emitted is not None:
            out.append(to_engine_candle(emitted))
    for emitted in resampler.flush(require_full=False):
        out.append(to_engine_candle(emitted))
    return out


async def load_bars(
    db: AsyncSession, figi: str, interval: int, limit: int
) -> list[EngineCandle]:
    """Последние limit свечей figi, по возрастанию ts (как в legacy)."""
    rows = (
        await db.execute(
            select(Candle)
            .where(Candle.figi == figi, Candle.interval == interval)
            .order_by(Candle.ts.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [to_engine_candle(r) for r in reversed(rows)]


async def load_all_bars(
    db: AsyncSession, figi: str, interval: int
) -> list[EngineCandle]:
    """Все свечи figi за интервал, по возрастанию ts, без ограничения выборки."""
    rows = (
        await db.execute(
            select(Candle)
            .where(Candle.figi == figi, Candle.interval == interval)
            .order_by(Candle.ts)
        )
    ).scalars().all()
    return [to_engine_candle(r) for r in rows]
