from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
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

# Кэш ответов анализа: переключение графиков/авто-refresh не должны ждать
# пересчёт индикаторов (на загруженной машине ~2.5с на запрос).
_analysis_cache: dict[tuple, tuple[float, dict]] = {}
_ANALYSIS_CACHE_TTL = 20.0


def _cache_get(key: tuple) -> dict | None:
    item = _analysis_cache.get(key)
    if item is not None and (_time.monotonic() - item[0]) < _ANALYSIS_CACHE_TTL:
        return item[1]
    return None


def _cache_put(key: tuple, value: dict) -> None:
    if len(_analysis_cache) > 300:
        _analysis_cache.clear()
    _analysis_cache[key] = (_time.monotonic(), value)


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


# Интрадей-ТФ, которые можно досчитывать из минутных свечей (без T-Invest API).
# day/week/month не трогаем: у них свои таблицы и свежая история.
_TF_REFRESHABLE = {"5min", "10min", "15min", "hour", "2h", "4h"}
_bg_tf_ensure: dict[tuple, asyncio.Task] = {}


async def _spawn_ensure_tf(figi: str, interval_name: str, limit: int) -> None:
    """Фоновая дотяжка просевшего ТФ из 1m: заполняет пропуск [последний бар; сейчас].

    Не блокирует HTTP-ответ. Повторно не запускается, пока таск жив.
    """
    key = (figi, interval_name)
    existing = _bg_tf_ensure.get(key)
    if existing is not None and not existing.done():
        return

    async def _worker() -> None:
        from app.database import SessionLocal
        from app.services.candle_cache import _INTERVAL_STEP_SEC, ensure_candles

        step_sec = _INTERVAL_STEP_SEC.get(interval_name) or 3600
        days = max(30, int(limit * step_sec / 86400) + 5)
        try:
            async with SessionLocal() as db:
                res = await asyncio.wait_for(
                    ensure_candles(db, figi, interval_name, days=days,
                                   range_to=datetime.now(_tz.utc)),
                    timeout=25,
                )
                logger.info("ensure_tf %s %s: down=%s req=%s", figi[-6:], interval_name,
                            res.get("downloaded"), res.get("requests"))
        except asyncio.TimeoutError:
            logger.warning("ensure_tf %s %s timeout(25s)", figi[-6:], interval_name)
        except Exception as e:
            logger.warning("ensure_tf %s %s err %s: %s", figi[-6:], interval_name,
                           type(e).__name__, str(e)[:100])

    task = asyncio.create_task(_worker())
    _bg_tf_ensure[key] = task


async def _maybe_spawn_ensure_tf(db: AsyncSession, figi: str, interval_name: str, limit: int) -> None:
    """Запускает _spawn_ensure_tf, если таблица ТФ отстала от живого 1m более чем на ~2 шага."""
    from app.services.candle_cache import _INTERVAL_STEP_SEC

    step_sec = _INTERVAL_STEP_SEC.get(interval_name)
    one = INTERVAL_NAMES.get("1min")
    target = INTERVAL_NAMES.get(interval_name)
    if step_sec is None or one is None or target is None:
        return
    try:
        one_value = int(one.value)
        target_value = int(target.value)
        one_max = (await db.execute(
            select(func.max(Candle.ts)).where(Candle.figi == figi, Candle.interval == one_value)
        )).scalar_one_or_none()
        tf_max = (await db.execute(
            select(func.max(Candle.ts)).where(Candle.figi == figi, Candle.interval == target_value)
        )).scalar_one_or_none()
        if one_max is None:
            return
        # дотяжка, только если ТФ отстала от живого 1m более чем на 1 шаг
        if tf_max is not None and (one_max - tf_max).total_seconds() <= step_sec:
            return
    except Exception as e:
        logger.debug("tf staleness check %s %s: %s", figi[-6:], interval_name, e)
        return
    await _spawn_ensure_tf(figi, interval_name, limit)


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
    before_ts: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    interval = INTERVAL_NAMES.get(interval_name)
    if interval is None:
        raise HTTPException(400, f"Unknown interval. Available: {', '.join(INTERVAL_NAMES)}")

    # before_ts: окно заканчивается на указанном времени (для тестов/реплея —
    # иначе «последние N баров» уходят в сегодня и историческое окно не видно).
    _before_dt = None
    if before_ts:
        try:
            _before_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
            if _before_dt.tzinfo is None:
                _before_dt = _before_dt.replace(tzinfo=_tz.utc)
        except Exception:
            _before_dt = None

    cache_key = (figi, interval_name, int(limit), _before_dt.isoformat() if _before_dt else "")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

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

    # Старший интрадей-ТФ отстал от «живого» 1m (15m/1h/2h и т.п. не обновлялись
    # автоматически) — фоново дотягиваем из минутных свечей, не блокируя ответ.
    # Для before_ts (история/реплей) дотяжку не делаем.
    if not _before_dt and interval_name in _TF_REFRESHABLE:
        await _maybe_spawn_ensure_tf(db, resolved_figi, interval_name, limit)

    stmt = (
        select(Candle)
        .where(Candle.figi == resolved_figi, Candle.interval == int(getattr(interval, "value", interval)))
        .order_by(Candle.ts.desc())
        .limit(limit)
    )
    if _before_dt is not None:
        stmt = stmt.where(Candle.ts <= _before_dt)
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
        empty = {
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
        _cache_put(cache_key, empty)
        return empty

    closes = [float(c.close) for c in candles]
    sma20 = sma(closes, 20)
    ema50_series = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
    # MACD: параметры из optuna (strategy_params.macd_cross) для конкретного тикера,
    # иначе дефолт 12/26/9. Данные лежат в instruments.optuna_params (JSONB).
    m_fast, m_slow, m_sig = 12, 26, 9
    try:
        _opt_row = (await db.execute(
            _text("SELECT optuna_params FROM instruments WHERE figi = :f"),
            {"f": resolved_figi},
        )).first()
        _opt = (_opt_row[0] if _opt_row else None) or {}
        _sp = (_opt.get("strategy_params") or {}).get("macd_cross") or {}
        m_fast = int(_sp.get("fast", 12))
        m_slow = int(_sp.get("slow", 26))
        m_sig = int(_sp.get("signal_period", 9))
    except Exception:
        pass
    m_line, s_line, hist = macd(closes, fast=m_fast, slow=m_slow, signal_period=m_sig)

    payload = {
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
        "macd_params": {"fast": m_fast, "slow": m_slow, "signal_period": m_sig},
    }
    _cache_put(cache_key, payload)
    return payload
