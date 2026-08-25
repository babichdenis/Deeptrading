from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candle import Candle
from app.models.instrument import Instrument
from app.services.indicators import ema, macd, rsi, sma
from app.services.tinvest import INTERVAL_NAMES

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


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

    instrument = await db.scalar(select(Instrument).where(Instrument.figi == figi))
    if not instrument:
        raise HTTPException(404, "Instrument not found, call /api/instruments/sync first")

    stmt = (
        select(Candle)
        .where(Candle.figi == figi, Candle.interval == int(getattr(interval, "value", interval)))
        .order_by(Candle.ts.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    candles = list(reversed(result.scalars().all()))
    if not candles:
        return {
            "figi": figi,
            "ticker": instrument.ticker,
            "name": instrument.name,
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
        "ticker": instrument.ticker,
        "name": instrument.name,
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
