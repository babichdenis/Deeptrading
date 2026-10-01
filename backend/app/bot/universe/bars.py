"""Доступность и загрузка баров, ресемпл 1m → 5m.

Ресемплинг идёт через канонический app.marketdata.resampler.Resampler
(тот же агрегатор, что у ReplayFeed), а не через локальную копию: это
гарантирует, что ATR в Universe считается по тем же бакетам, что и движок.

Загрузка для Universe двух видов:
    load_bars / load_all_bars — один FIGI (совместимость, legacy-путь);
    load_bars_bulk_bounded   — хвост окна для всего рынка одним запросом.

Bounded-семантика (AUDIT P1.1): меры (volatility/trend/features) читают только
visible[-window:] и последний close, где visible = бары с ts <= as_of. Поэтому
загрузка последних window баров с ts <= as_of даёт Побитово тот же результат,
что и вся история, — но вместо ~3000 баров на FIGI едет ~44. Эквивалентность
зафиксирована тестом test_universe_v2_bulk_load.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import DateTime, Integer, SmallInteger, String, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feed import ClosedCandle
from app.engine.models import Candle as EngineCandle
from app.marketdata.resampler import Resampler
from app.models.candle import Candle

RESAMPLE_TARGET = "5min"
RESAMPLE_PERIOD_MIN = 5


VISIBLE_INTERVAL_MIN = 5


def bar_close_ts(bar, interval: timedelta) -> datetime:
    """Момент закрытия бара = его метка + интервал.

    Метка бара — НАЧАЛО бакета (канон проекта, как в T-Invest и в
    app.marketdata.resampler: «ts закрытого бара = начало бакета»), поэтому
    бар с меткой T описывает [T, T+interval) и закрывается в T+interval.
    """
    return bar.ts + interval


def bar_is_visible(bar, *, as_of: datetime, interval: timedelta) -> bool:
    """Виден ли ЗАКРЫТЫЙ бар на момент as_of.

    AUDIT P1.3: сравнение `ts <= as_of` на START-метках пропускает незакрытый
    бар — тот, чей интервал [ts, ts+interval) ещё не истёк, то есть заглядывает
    в его внутренность. Видимым бар считается по факту закрытия: ts + interval
    <= as_of. Граница включительная — на самом моменте закрытия бар виден.
    """
    return bar_close_ts(bar, interval) <= as_of


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


BULK_TAIL_SQL = text(
    """
    SELECT f.figi AS figi, c.ts, c.open, c.high, c.low, c.close, c.volume
    FROM unnest(:figis) AS f(figi)
    CROSS JOIN LATERAL (
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE candles.figi = f.figi
          AND candles.interval = :interval
          AND candles.ts <= :as_of
        ORDER BY candles.ts DESC
        LIMIT :window
    ) AS c
    ORDER BY f.figi, c.ts
    """
).bindparams(
    bindparam("figis", type_=ARRAY(String)),
    bindparam("interval", type_=SmallInteger),
    bindparam("as_of", type_=DateTime(timezone=True)),
    bindparam("window", type_=Integer),
)


def _as_tail_rows(result) -> list:
    """Строки результата как кортежи (figi, ts, open, high, low, close, volume).

    Приводит любой flavour результата (Row, Mapping, tuple) к единому виду:
    драйверы отдают строки по-разному, а группировке нужен один контракт.
    """
    rows = result.all() if hasattr(result, "all") else list(result)
    return [tuple(r) for r in rows]


def group_tail_rows(rows: Sequence) -> dict[str, list[EngineCandle]]:
    """Раскладывает строки BULK_TAIL_SQL по FIGI в возрастающий ts.

    Чистая функция: принимает кортежи (figi, ts, open, high, low, close,
    volume). Порядок строк в SQL уже гарантирует возрастание ts внутри FIGI,
    но группировка не полагается на это — бары сортируются здесь, чтобы
    инвариант «bars_by_figi[f] отсортирован по ts» держался при любом плане.
    """
    out: dict[str, list[EngineCandle]] = {}
    for row in rows:
        figi, ts, open_, high, low, close, volume = row[:7]
        out.setdefault(figi, []).append(
            EngineCandle(
                ts=ts,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )
    for bars in out.values():
        bars.sort(key=lambda b: b.ts)
    return out


async def load_bars_bulk_bounded(
    db: AsyncSession,
    figis: Sequence[str],
    interval: int,
    *,
    window: int,
    as_of: datetime,
) -> dict[str, list[EngineCandle]]:
    """Последние window свечей с ts <= as_of для каждого FIGI, одним запросом.

    Заменяет N вызовов load_all_bars (каждый тянул всю историю инструмента).
    Ключ возврата — figi; FIGI без баров на as_of отсутствует, как и раньше.

    window должен быть >= FEATURE_WINDOW: меньшее окно тихо урежет вход мер.
    Пустой figis не ходит в БД.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    unique = sorted({f.strip() for f in figis if f and f.strip()})
    if not unique:
        return {}
    result = await db.execute(
        BULK_TAIL_SQL,
        {"figis": unique, "interval": interval, "as_of": as_of, "window": window},
    )
    return group_tail_rows(_as_tail_rows(result))


def tail_figis(unique_figis: Sequence[str], bars_by_figi: dict[str, list]) -> list[str]:
    """FIGI, у которых есть хоть один видимый бар (для отладки/логов)."""
    return [f for f in unique_figis if bars_by_figi.get(f)]
