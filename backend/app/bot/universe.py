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


async def select_eligible_universe(db: AsyncSession, top_n: int = 20) -> list[dict]:
    """Загружает eligible-тикеры из таблицы universe (tier=eligible)."""
    from app.models.instrument import Instrument

    rows = (await db.execute(
        text("SELECT figi, ticker, lot_size, avg_price, avg_daily_turnover, sector "
             "FROM universe WHERE eligible_tier = 'eligible' ORDER BY ticker")
    )).all()

    if not rows:
        return []

    # Проверяем наличие свечей в БД (1m достаточно — бот ресемплит в 5m сам)
    figi_list = [r[0] for r in rows]
    recent = (await db.execute(
        select(Candle.figi)
        .where(Candle.figi.in_(figi_list), Candle.interval == 1)
        .group_by(Candle.figi)
        .having(func.count(Candle.ts) >= 1)
    )).scalars().all()
    has_data = set(recent)

    # Загружаем ATR% для каждого тикера
    from app.engine.indicators import atr as atr_series
    from app.engine.models import Candle as EC

    result = []
    for figi, ticker, lot, price, turnover, sector in rows:
        if figi not in has_data:
            continue

        # 5m из 1m на лету для ATR (последние 200 1m-баров)
        bars = (
            await db.execute(
                select(Candle)
                .where(Candle.figi == figi, Candle.interval == 1)
                .order_by(Candle.ts.desc())
                .limit(1000)
            )
        ).scalars().all()
        if len(bars) < 5:
            continue

        # ресемпл 1m → 5m в памяти
        from app.engine.models import Candle as EC5
        _by5 = {}
        for b in reversed(bars):
            key = int(b.ts.timestamp() // 300)
            if key not in _by5:
                _by5[key] = {"ts": datetime.fromtimestamp(key * 300, tz=timezone.utc),
                             "open": float(b.open), "high": float(b.high),
                             "low": float(b.low), "close": float(b.close), "volume": float(b.volume)}
            else:
                g = _by5[key]
                g["high"] = max(g["high"], float(b.high))
                g["low"] = min(g["low"], float(b.low))
                g["close"] = float(b.close)
                g["volume"] += float(b.volume)
        ec = [EC5(ts=g["ts"], open=g["open"], high=g["high"], low=g["low"],
                  close=g["close"], volume=g["volume"]) for g in _by5.values()]
        if len(ec) < 15:
            continue
        values = atr_series(ec[-44:], 14)
        last_atr = next((v for v in reversed(values) if v is not None), None)

        result.append({
            "figi": figi,
            "ticker": ticker,
            "lot": int(lot) if lot else 10,
            "name": ticker,
            "atr_pct": round(last_atr / ec[-1].close * 100, 3) if last_atr and ec[-1].close else 0,
            "avg_price": float(price) if price else 0,
            "avg_turnover": float(turnover) if turnover else 0,
            "sector": sector or "",
        })

    # Сортируем по ATR% (как и старая функция)
    result.sort(key=lambda x: x["atr_pct"], reverse=True)
    return result[:top_n]


async def select_all_tradeable(db: AsyncSession, top_n: int = 6) -> list[dict]:
    """Top-N бумаг по ATR% волатильности из instrument_info."""
    from app.models.instrument import Instrument
    from app.models.candle import Candle

    res = await db.execute(
        select(Instrument.figi, Instrument.ticker, Instrument.name)
    )
    by_figi: dict[str, tuple] = {}
    for figi, ticker, name in res.all():
        if figi:
            by_figi[figi] = (ticker, name)

    rows = (await db.execute(
        text("SELECT figi, ticker, lot, name FROM instrument_info "
             "WHERE figi IS NOT NULL AND api_trade_available=true")
    )).all()
    candidates = []
    for figi, ticker, lot, name in rows:
        candidates.append({
            "figi": figi,
            "ticker": ticker or (by_figi.get(figi) or ("", ""))[0],
            "lot": int(lot) if lot else 10,
            "name": name or (by_figi.get(figi) or ("", ""))[1],
        })

    figi_list = [c["figi"] for c in candidates]
    if not figi_list:
        return candidates

    recent = (await db.execute(
        select(Candle.figi)
        .where(Candle.figi.in_(figi_list), Candle.interval == 5)
        .group_by(Candle.figi)
        .having(func.count(Candle.ts) >= 30)
    )).scalars().all()
    has_data = set(recent)

    scored = []
    from app.engine.indicators import atr as atr_series
    from app.engine.models import Candle as EC
    for c in candidates:
        if c["figi"] not in has_data:
            continue
        bars = (
            await db.execute(
                select(Candle)
                .where(Candle.figi == c["figi"], Candle.interval == 5)
                .order_by(Candle.ts.desc())
                .limit(200)
            )
        ).scalars().all()
        if len(bars) < 30:
            continue
        bars = list(reversed(bars))
        ec = [EC(ts=b.ts, open=float(b.open), high=float(b.high),
                 low=float(b.low), close=float(b.close), volume=float(b.volume))
              for b in bars]
        values = atr_series(ec[-44:], 14)
        last_atr = next((v for v in reversed(values) if v is not None), None)
        if not last_atr or ec[-1].close == 0:
            continue
        c["atr_pct"] = round(last_atr / ec[-1].close * 100, 3)
        scored.append(c)

    scored.sort(key=lambda x: x["atr_pct"], reverse=True)
    return scored[:top_n]


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
