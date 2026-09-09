from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candle import Candle
from app.models.instrument import Instrument
from app.services.indicators import ema, macd, rsi, sma
from app.services.tinvest import INTERVAL_NAMES

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta, timezone as _tz

logger = logging.getLogger("analysis_1min")

# JIT/фоновая докачка 1min: НЕ блокируем HTTP-ответ API-вызовом (он медленный),
# а запускаем ensure_candles отдельным таском. Ответ фронту — из БД мгновенно.
_bg_ensure: dict[str, asyncio.Task] = {}


async def _spawn_ensure_1min(figi: str) -> None:
    """Фоновая докачка последних 2 часов 1min из T-Invest API (без блокировки запроса)."""
    existing = _bg_ensure.get(figi)
    if existing is not None and not existing.done():
        return

    async def _worker() -> None:
        from app.database import SessionLocal
        from app.services.candle_cache import ensure_candles

        to_ = datetime.now(_tz.utc)
        from_ = to_ - timedelta(hours=2)
        try:
            async with SessionLocal() as db:
                res = await asyncio.wait_for(
                    ensure_candles(db, figi, "1min", range_from=from_, range_to=to_),
                    timeout=12,
                )
                logger.info("ensure_1min %s down=%s req=%s", figi[-6:],
                            res.get("downloaded"), res.get("requests"))
        except asyncio.TimeoutError:
            logger.warning("ensure_1min %s timeout(12s)", figi[-6:])
        except Exception as e:
            logger.warning("ensure_1min %s err %s: %s", figi[-6:], type(e).__name__, str(e)[:100])

    task = asyncio.create_task(_worker())
    _bg_ensure[figi] = task


def _bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> tuple[list, list]:
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        std = var**0.5
        upper[i] = mean + k * std
        lower[i] = mean - k * std
    return upper, lower


@router.get("/{figi}")
async def get_analysis(
    figi: str,
    interval_name: str = Query("day"),
    limit: int = Query(1500, ge=10, le=5000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    interval = INTERVAL_NAMES.get(interval_name)
    if interval is None:
        raise HTTPException(400, f"Unknown interval. Available: {', '.join(INTERVAL_NAMES)}")

    from sqlalchemy import text as _text
    instrument = await db.scalar(select(Instrument).where(Instrument.figi == figi))
    fallback_ticker = None
    if not instrument:
        # TCS figi (sandbox) -> ticker -> BBG figi
        ticker_row = await db.execute(_text("SELECT ticker FROM instrument_info WHERE figi = :f"), {"f": figi})
        fallback_ticker = ticker_row.scalar()
        if fallback_ticker:
            instrument = await db.scalar(select(Instrument).where(Instrument.ticker == fallback_ticker))
    resolved_figi = instrument.figi if instrument else figi
    name = (instrument.name if instrument else "") or fallback_ticker or figi[:8]
    ticker_name = (instrument.ticker if instrument else "") or fallback_ticker or figi[:8]

    interval_value = int(getattr(interval, "value", interval))

    stmt = (
        select(Candle)
        .where(Candle.figi == resolved_figi, Candle.interval == int(getattr(interval, "value", interval)))
        .order_by(Candle.ts.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    candles = list(reversed(result.scalars().all()))
    # Для 1min — если последний бар в БД старше 10 минут, запускаем фоновую
    # докачку (НЕ блокируя ответ). Ответ отдаём из БД как есть.
    if interval_value == 1 and candles:
        last_ts = candles[-1].ts
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=_tz.utc)
        try:
            age_min = (datetime.now(_tz.utc) - last_ts).total_seconds() / 60
            if age_min > 10:
                await _spawn_ensure_1min(resolved_figi)
        except Exception:
            pass
    if not candles:
        return {
            "figi": figi,
            "ticker": ticker_name,
            "name": name,
            "interval": interval_name,
            "candles": [],
            "sma20": [],
            "ema50": [],
            "rsi": [],
            "bb_upper": [],
            "bb_lower": [],
            "macd": {"macd": [], "signal": [], "hist": []},
        }

    closes = [float(c.close) for c in candles]
    sma20 = sma(closes, 20)
    ema50_series = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
    m_line, s_line, hist = macd(closes)

    return {
        "figi": figi,
        "ticker": ticker_name,
        "name": name,
        "interval": interval_name,
        "candles": [
            {
                "ts": c.ts.isoformat(),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": c.volume,
            }
            for c in candles
        ],
        "sma20": sma20,
        "ema50": ema50_series,
        "rsi": rsi14,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "macd": {"macd": m_line, "signal": s_line, "hist": hist},
    }
