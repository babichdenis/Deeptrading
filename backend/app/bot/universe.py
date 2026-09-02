from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.indicators import atr as atr_series
from app.models.candle import Candle
from app.services.candle_cache import ensure_candles

LIQUID_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "MTSS", "TATN", "MGNT",
    "CHMF", "ALRS", "PLZL", "SNGS", "VTBR", "AFLT", "PHOR",
]


async def select_all_tradeable(db: AsyncSession) -> list[dict]:
    """Все бумаги с api_trade_available=true из instrument_info (как файлик-бот)."""
    from app.models.instrument import Instrument
    from app.database import SessionLocal

    res = await db.execute(
        select(Instrument.figi, Instrument.ticker, Instrument.name)
    )
    by_figi: dict[str, tuple] = {}
    for figi, ticker, name in res.all():
        if figi:
            by_figi[figi] = (ticker, name)

    rows = (await db.execute(
        text("SELECT figi, ticker, lot, name FROM instrument_info "
             "WHERE figi IS NOT NULL AND api_trade_available=true ORDER BY ticker")
    )).all()
    out = []
    for figi, ticker, lot, name in rows:
        out.append({
            "figi": figi,
            "ticker": ticker or (by_figi.get(figi) or ("", ""))[0],
            "lot": int(lot) if lot else 10,
            "name": name or (by_figi.get(figi) or ("", ""))[1],
        })
    return out


async def select_volatile_universe(
    db: AsyncSession,
    figi_by_ticker: dict[str, str],
    top_n: int = 6,
    days: int = 45,
    min_bars: int = 20,
) -> list[dict]:
    ranked: list[dict] = []
    for ticker in LIQUID_TICKERS:
        figi = figi_by_ticker.get(ticker)
        if not figi:
            continue
        await ensure_candles(db, figi, "day", days)
        rows = (
            await db.execute(
                select(Candle)
                .where(Candle.figi == figi, Candle.interval == 5)
                .order_by(Candle.ts)
            )
        ).scalars().all()
        if len(rows) < min_bars:
            continue
        bars = [
            __import__("app.engine.models", fromlist=["Candle"]).Candle(
                ts=r.ts, open=float(r.open), high=float(r.high),
                low=float(r.low), close=float(r.close), volume=float(r.volume),
            )
            for r in rows
        ]
        values = atr_series(bars[-min_bars - 14 :], 14)
        last_atr = next((v for v in reversed(values) if v is not None), None)
        if not last_atr or bars[-1].close == 0:
            continue
        turnover = sum(float(c.volume) * float(c.close) for c in bars[-10:]) / 10
        ranked.append(
            {
                "figi": figi,
                "ticker": ticker,
                "atr_pct": round(last_atr / bars[-1].close * 100, 3),
                "avg_turnover": round(turnover, 0),
            }
        )
    ranked.sort(key=lambda x: x["atr_pct"], reverse=True)
    return ranked[:top_n]


async def count_day_candles(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(Candle)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def session_date(ts: datetime) -> str:
    return ts.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Moscow")).date().isoformat()


def align_step(step_sec: int) -> datetime:
    now = datetime.now(timezone.utc)
    epoch = now.timestamp() // step_sec * step_sec
