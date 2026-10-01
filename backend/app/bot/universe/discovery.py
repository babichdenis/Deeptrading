"""Universe: слой обнаружения кандидатов.

Отвечает на вопрос «кого вообще рассматриваем» и «есть ли по нему данные».
Не знает про ATR, Top-N и веса. Три источника кандидатов — ровно те, что
были в app.bot.universe до рефакторинга.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from .domain import InstrumentRef, UniverseEntry, UniverseSnapshot, UniverseSource

LIQUID_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "MTSS", "TATN", "MGNT",
    "CHMF", "ALRS", "PLZL", "SNGS", "VTBR", "AFLT", "PHOR",
]

DEFAULT_LOT = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value, default: int = DEFAULT_LOT) -> int:
    return int(value) if value else default


def _as_float(value) -> float:
    return float(value) if value else 0


async def discover_eligible_universe(db: AsyncSession) -> UniverseSnapshot:
    """Таблица universe, tier = 'eligible'. Порядок — по тикеру (ORDER BY)."""
    rows = (
        await db.execute(
            text(
                "SELECT figi, ticker, lot_size, avg_price, avg_daily_turnover, sector "
                "FROM universe WHERE eligible_tier = 'eligible' ORDER BY ticker"
            )
        )
    ).all()
    return UniverseSnapshot(
        as_of=_utc_now(),
        source=UniverseSource.ELIGIBLE_TABLE,
        entries=tuple(
            UniverseEntry(
                ref=InstrumentRef(ticker=ticker, figi=figi),
                lot=_as_int(lot),
                name=ticker,
                avg_price=_as_float(price),
                avg_turnover=_as_float(turnover),
                sector=sector or "",
            )
            for figi, ticker, lot, price, turnover, sector in rows
        ),
    )


async def discover_tradeable_universe(db: AsyncSession) -> UniverseSnapshot:
    """instrument_info с api_trade_available = true.

    Тикер и имя берутся из instrument_info, а при пустых значениях —
    добираются из instruments. Порядок строк БД сохраняется как есть.
    """
    from app.models.instrument import Instrument

    by_figi: dict[str, tuple] = {}
    for figi, ticker, name in (await db.execute(
        select(Instrument.figi, Instrument.ticker, Instrument.name)
    )).all():
        if figi:
            by_figi[figi] = (ticker, name)

    rows = (
        await db.execute(
            text(
                "SELECT figi, ticker, lot, name FROM instrument_info "
                "WHERE figi IS NOT NULL AND api_trade_available=true"
            )
        )
    ).all()

    entries = []
    for figi, ticker, lot, name in rows:
        fallback = by_figi.get(figi) or ("", "")
        entries.append(
            UniverseEntry(
                ref=InstrumentRef(ticker=ticker or fallback[0], figi=figi),
                lot=_as_int(lot),
                name=name or fallback[1],
            )
        )
    return UniverseSnapshot(
        as_of=_utc_now(),
        source=UniverseSource.INSTRUMENT_INFO,
        entries=tuple(entries),
    )


def discover_liquid_universe(
    figi_by_ticker: dict[str, str], tickers: list[str] | None = None
) -> UniverseSnapshot:
    """Статический список ликвидных тикеров, привязанный к figi.

    Тикеры без figi отбрасываются на этом же слое, как и в legacy.
    """
    entries = []
    for ticker in tickers if tickers is not None else LIQUID_TICKERS:
        figi = figi_by_ticker.get(ticker)
        if not figi:
            continue
        entries.append(
            UniverseEntry(ref=InstrumentRef(ticker=ticker, figi=figi), name=ticker)
        )
    return UniverseSnapshot(
        as_of=_utc_now(),
        source=UniverseSource.LIQUID_TICKERS,
        entries=tuple(entries),
    )


async def count_bars_by_figi(
    db: AsyncSession, figis: list[str], interval: int, min_bars: int = 1
) -> set[str]:
    """FIGI, у которых в БД есть хотя бы min_bars свечей заданного интервала.

    Пред-фильтр перед загрузкой баров: не тянем историю для инструментов,
    которым она заведомо не нужна.
    """
    if not figis:
        return set()

    from app.models.candle import Candle

    rows = (
        await db.execute(
            select(Candle.figi)
            .where(Candle.figi.in_(figis), Candle.interval == interval)
            .group_by(Candle.figi)
            .having(func.count(Candle.ts) >= min_bars)
        )
    ).scalars().all()
    return set(rows)


async def fetch_lot_by_figi(db: AsyncSession, figis: list[str]) -> dict[str, int]:
    """Реальный размер лота из instrument_info для переданных FIGI.

    Нужен там, где источник Universe не несёт лот (ликвидный список задан
    тикерами, а не выборкой из БД) — иначе в выдаче оказывался хардкод 10.
    FIGI без строки в instrument_info не попадает в результат: вызывающий код
    решает, чем заменить неизвестный лот (DEFAULT_LOT), молчаливого нуля нет.
    """
    wanted = sorted({f.strip() for f in figis if f and f.strip()})
    if not wanted:
        return {}

    rows = (
        await db.execute(
            text("SELECT figi, lot FROM instrument_info WHERE figi = ANY(:figis)").bindparams(
                bindparam("figis", type_=ARRAY(String))
            ),
            {"figis": wanted},
        )
    ).all()
    return {figi: _as_int(lot) for figi, lot in rows if figi}
