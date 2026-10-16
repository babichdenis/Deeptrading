import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from t_tech.invest import CandleInterval, Client
from t_tech.invest.utils import quotation_to_decimal

from app.config import get_settings
from app.models.candle import Candle
from app.models.instrument import Instrument
from app.services import candle_ingestion_contract as cic
from app.services import candle_integrity

INTERVAL_NAMES: dict[str, CandleInterval] = {
    "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "10min": CandleInterval.CANDLE_INTERVAL_10_MIN,
    "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
    "hour": CandleInterval.CANDLE_INTERVAL_HOUR,
    "2h": CandleInterval.CANDLE_INTERVAL_2_HOUR,
    "4h": CandleInterval.CANDLE_INTERVAL_4_HOUR,
    "day": CandleInterval.CANDLE_INTERVAL_DAY,
    "week": CandleInterval.CANDLE_INTERVAL_WEEK,
    "month": CandleInterval.CANDLE_INTERVAL_MONTH,
}

_CHUNK_DAYS = {
    CandleInterval.CANDLE_INTERVAL_DAY: 365,
    CandleInterval.CANDLE_INTERVAL_WEEK: 3650,
    CandleInterval.CANDLE_INTERVAL_MONTH: 7300,
    CandleInterval.CANDLE_INTERVAL_4_HOUR: 90,
    CandleInterval.CANDLE_INTERVAL_2_HOUR: 60,
    CandleInterval.CANDLE_INTERVAL_HOUR: 31,
    CandleInterval.CANDLE_INTERVAL_30_MIN: 31,
    CandleInterval.CANDLE_INTERVAL_15_MIN: 7,
    CandleInterval.CANDLE_INTERVAL_10_MIN: 7,
    CandleInterval.CANDLE_INTERVAL_5_MIN: 7,
    CandleInterval.CANDLE_INTERVAL_1_MIN: 1,
}


def _interval_value(interval: CandleInterval) -> int:
    return int(getattr(interval, "value", interval))


async def to_thread(func, *args):
    return await asyncio.to_thread(func, *args)


def _q2f(q) -> float:
    return q.units + q.nano / 1e9


def fetch_shares() -> list[dict]:
    settings = get_settings()
    with Client(settings.tinkoff_token) as client:
        response = client.instruments.shares()
    return [
        {
            "figi": s.figi,
            "ticker": s.ticker,
            "class_code": s.class_code,
            "isin": s.isin or None,
            "name": s.name,
            "currency": s.currency,
            "sector": s.sector or None,
            "lot": s.lot,
            "dlong": _q2f(s.dlong),
            "dshort": _q2f(s.dshort),
            "long_lev": round(1.0 / _q2f(s.dlong), 2) if _q2f(s.dlong) > 0 else 0,
            "short_lev": round(1.0 / _q2f(s.dshort), 2) if _q2f(s.dshort) > 0 else 0,
        }
        for s in response.instruments
        if s.api_trade_available_flag and s.figi
    ]


def fetch_candles(
    figi: str, interval: CandleInterval, date_from: datetime, date_to: datetime
) -> list[dict]:
    settings = get_settings()
    chunk_days = _CHUNK_DAYS.get(interval, 365)
    interval_value = _interval_value(interval)
    rows_by_key: dict[tuple, dict] = {}
    with Client(settings.tinkoff_token) as client:
        cursor = date_from
        while cursor < date_to:
            chunk_end = min(cursor + timedelta(days=chunk_days), date_to)
            response = client.market_data.get_candles(
                figi=figi,
                interval=interval,
                from_=cursor,
                to=chunk_end,
            )
            for c in response.candles:
                key = (figi, interval_value, c.time)
                rows_by_key[key] = {
                    "figi": figi,
                    "interval": interval_value,
                    "ts": c.time,
                    "open": quotation_to_decimal(c.open),
                    "high": quotation_to_decimal(c.high),
                    "low": quotation_to_decimal(c.low),
                    "close": quotation_to_decimal(c.close),
                    "volume": c.volume,
                }
            cursor = chunk_end
    return sorted(rows_by_key.values(), key=lambda r: r["ts"])


async def upsert_instruments(db: AsyncSession, items: list[dict]) -> int:
    if not items:
        return 0
    stmt = pg_insert(Instrument).values(items)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Instrument.figi],
        set_={
            "ticker": stmt.excluded.ticker,
            "class_code": stmt.excluded.class_code,
            "isin": stmt.excluded.isin,
            "name": stmt.excluded.name,
            "currency": stmt.excluded.currency,
            "sector": stmt.excluded.sector,
            "lot": stmt.excluded.lot,
            "dlong": stmt.excluded.dlong,
            "dshort": stmt.excluded.dshort,
            "long_lev": stmt.excluded.long_lev,
            "short_lev": stmt.excluded.short_lev,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(items)


async def upsert_candles(db: AsyncSession, rows: list[dict], *,
                         window: tuple[datetime, datetime] | None = None) -> int:
    """Записать пакет свечей с контрактной проверкой. Возвращает число принятых
    уникальных валидных строк (совместимо с прежним int-контрактом)."""
    res = await upsert_candles_detailed(db, rows, window=window)
    return res.accepted


async def upsert_candles_detailed(db: AsyncSession, rows: list[dict], *,
                                  window: tuple[datetime, datetime] | None = None
                                  ) -> cic.UpsertResult:
    """Атомарная запись пакета с полным различием inserted/unchanged/corrected.

    Владение транзакцией: функция выполняет validate+dedup -> pre-SELECT ->
    запись -> invalidate -> commit в ОДНОЙ транзакции сессии `db`. Частичного
    COMMIT нет: любая ошибка (валидация/конфликт/запись/invalidate) прерывает
    ДО commit. При ошибке функция САМА откатывает транзакцию (rollback), чтобы
    вызывающий не мог закоммитить неуспешный пакет при переиспользовании сессии.
    Ошибки invalidate НЕ подавляются — пробрасываются вызывающему.
    """
    try:
        clean = cic.assert_valid_batch(list(rows), window=window)
        if not clean:
            return cic.UpsertResult()

        # pre-SELECT существующих строк по диапазону ts на figi+interval (эффективно по индексу,
        # без громоздкого IN-списка на больших бэктфиллах); точное совпадение — по natural key.
        groups: dict[tuple, list[dict]] = {}
        for r in clean:
            groups.setdefault((r["figi"], int(r["interval"])), []).append(r)
        emap = {}
        for (figi, ival), grp in groups.items():
            lo = min(cic.utc_ts(r["ts"]) for r in grp)
            hi = max(cic.utc_ts(r["ts"]) for r in grp)
            existing = (await db.execute(
                select(Candle).where(
                    Candle.figi == figi, Candle.interval == ival,
                    Candle.ts >= lo, Candle.ts <= hi)
            )).scalars().all()
            for c in existing:
                emap[(c.figi, int(c.interval), cic.utc_ts(c.ts))] = c

        to_write: list[dict] = []
        inserted = unchanged = corrected = 0
        for r in clean:
            key = cic.row_key(r)
            cur = emap.get(key)
            if cur is None:
                inserted += 1
                to_write.append(r)
            elif cic.same_content(r, cur):
                unchanged += 1
            else:
                corrected += 1
                to_write.append(r)

        batch_size = 1000
        for i in range(0, len(to_write), batch_size):
            batch = to_write[i : i + batch_size]
            stmt = pg_insert(Candle).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_candles_figi_interval_ts",
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                },
            )
            await db.execute(stmt)

        # Инвалидация кэша целостности затронутых дней — в ТОЙ ЖЕ транзакции, без подавления.
        _days = sorted({(cic.utc_ts(r["ts"]) + timedelta(hours=3)).date() for r in clean})
        for (figs_g, _iv) in groups.keys():
            await candle_integrity.invalidate(db, [figs_g], _iv, _days)

        await db.commit()
        return cic.UpsertResult(
            accepted=len(clean), inserted=inserted, unchanged=unchanged,
            corrected=corrected, written=inserted + corrected, rejected=0,
        )
    except Exception:
        # Откатить незакоммиченные изменения, чтобы вызывающий не мог закоммитить
        # неуспешный пакет (например, invalidate упал после INSERT).
        try:
            await db.rollback()
        except Exception:
            pass
        raise


def decimal_default(value: Decimal) -> float:
    return float(value)
