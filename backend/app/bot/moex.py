from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text


_ENGINE_CACHE = None


def _sync_engine():
    global _ENGINE_CACHE
    if _ENGINE_CACHE is None:
        from app.config import get_settings

        s = get_settings()
        url = (f"postgresql://{s.postgres_user}:{s.postgres_password}"
               f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}")
        _ENGINE_CACHE = create_engine(
            url,
            pool_size=int(getattr(s, "db_pool_size", 10) or 10),
            max_overflow=int(getattr(s, "db_max_overflow", 20) or 20))
    return _ENGINE_CACHE


def moex_candles(ticker: str, from_date: str, till_date: str, interval: int = 1) -> list[dict]:
    """Свечи MOEX ISS (без T-Invest лимитов). interval: 1/10/60 мин, 24 = день, 7 = неделя."""
    url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
           f"/securities/{ticker}/candles.json?from={from_date}&till={till_date}&interval={int(interval)}")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            data = json.loads(urllib.request.urlopen(url, timeout=20).read())
            candles = data["candles"]
            cols = {n: i for i, n in enumerate(candles["columns"])}
            out = []
            for r in candles["data"]:
                ts = r[cols["begin"]]
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                out.append({
                    "open": r[cols["open"]],
                    "high": r[cols["high"]],
                    "low": r[cols["low"]],
                    "close": r[cols["close"]],
                    "volume": r[cols["volume"]],
                    "ts": ts,
                })
            return out
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 + attempt)  # 1с, 2с паузы между попытками
    raise last_err if last_err else RuntimeError("MOEX fetch failed")


IMOEX_FIGI = "BBG00KDWPPW2"
IMOEX_SECID = "IMOEX"
_MSK = timezone(timedelta(hours=3))


def imoex_candles(from_date: str, till_date: str) -> list[dict]:
    """1-мин свечи индекса IMOEX из MOEX ISS (index/boards/SNDX, с пагинацией).

    ISS отдаёт время в МСК → конвертируем в UTC (конвенция candles.ts).
    """
    base = (f"https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX"
            f"/securities/{IMOEX_SECID}/candles.json"
            f"?from={from_date}&till={till_date}&interval=1")
    out: list[dict] = []
    start = 0
    while True:
        data = json.loads(urllib.request.urlopen(f"{base}&start={start}", timeout=20).read())
        c = data["candles"]
        cols = {n: i for i, n in enumerate(c["columns"])}
        rows = c.get("data") or []
        for r in rows:
            ts = r[cols["begin"]]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts).replace(tzinfo=_MSK).astimezone(timezone.utc)
            out.append({
                "open": r[cols["open"]], "high": r[cols["high"]],
                "low": r[cols["low"]], "close": r[cols["close"]],
                "volume": int(r[cols["volume"]] or 0), "ts": ts,
            })
        if len(rows) < 500:
            break
        start += 500
    return out


def imoex_last_ts() -> datetime | None:
    """Последняя 1м свеча IMOEX в БД (или None)."""
    engine = _sync_engine()
    with engine.connect() as db:
        return db.execute(text(
            "SELECT max(ts) FROM candles WHERE figi=:f AND interval=1"
        ), {"f": IMOEX_FIGI}).scalar()


def _upsert_imoex(engine, candles: list[dict]) -> int:
    if not candles:
        return 0
    with engine.begin() as db:
        for c in candles:
            db.execute(text("""
                INSERT INTO candles (figi,interval,ts,open,high,low,close,volume)
                VALUES (:figi,1,:ts,:o,:h,:l,:c,:v)
                ON CONFLICT (figi,interval,ts) DO UPDATE
                SET open=:o,high=:h,low=:l,close=:c,volume=:v
            """), {"figi": IMOEX_FIGI, "ts": c["ts"], "o": c["open"], "h": c["high"],
                    "l": c["low"], "c": c["close"], "v": c["volume"]})
    return len(candles)


def sync_imoex_recent(minutes: int = 60) -> int:
    """Догрузить хвост 1м свечей IMOEX (последние `minutes` минут).

    ISS `till` — это начало дня (эксклюзивно), поэтому till = завтра (МСК),
    иначе за сегодняшний день возвращается пусто.
    """
    now = datetime.now(timezone.utc)
    from_ts = now - timedelta(minutes=max(5, int(minutes)))
    _msk = timezone(timedelta(hours=3))
    from_date = from_ts.astimezone(_msk).strftime("%Y-%m-%d")
    till_date = (now.astimezone(_msk) + timedelta(days=1)).strftime("%Y-%m-%d")
    candles = imoex_candles(from_date, till_date)
    tail = [c for c in candles if c["ts"] >= from_ts]
    return _upsert_imoex(_sync_engine(), tail)


def _sync_imoex_gap(days: int = 10) -> int:
    """Синхронный gap-fill: если хвост отстал >30 мин — догрузить от хвоста."""
    now = datetime.now(timezone.utc)
    last = imoex_last_ts()
    if last is not None and (now - last) <= timedelta(minutes=30):
        return 0
    from_ts = (last - timedelta(minutes=5)) if last is not None else (now - timedelta(days=max(1, days)))
    _msk = timezone(timedelta(hours=3))
    from_date = from_ts.astimezone(_msk).strftime("%Y-%m-%d")
    till_date = (now.astimezone(_msk) + timedelta(days=1)).strftime("%Y-%m-%d")
    candles = imoex_candles(from_date, till_date)
    tail = [c for c in candles if c["ts"] >= from_ts]
    return _upsert_imoex(_sync_engine(), tail)


def sync_imoex_sync(days: int = 10) -> int:
    now = datetime.now(timezone.utc)
    from_ = now - timedelta(days=days)
    _msk = timezone(timedelta(hours=3))
    candles = imoex_candles(from_.astimezone(_msk).strftime("%Y-%m-%d"),
                            (now.astimezone(_msk) + timedelta(days=1)).strftime("%Y-%m-%d"))
    return _upsert_imoex(_sync_engine(), candles)


async def ensure_imoex_candles(days: int = 10) -> int:
    """Догрузить 1м свечи IMOEX, если хвост в БД отстал (>30 мин) или данных нет."""
    import asyncio

    try:
        return await asyncio.to_thread(_sync_imoex_gap, days)
    except Exception as e:
        print(f"[moex] ensure_imoex_candles failed: {e}")
        return 0


def sync_moex_sync(figi: str, ticker: str, days: int = 10) -> int:
    now = datetime.now(timezone.utc)
    from_ = now - timedelta(days=days)
    candles = moex_candles(ticker, from_.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
    engine = _sync_engine()
    with engine.begin() as db:
            for c in candles:
                db.execute(text("""
                    INSERT INTO candles (figi,interval,ts,open,high,low,close,volume)
                    VALUES (:figi,1,:ts,:o,:h,:l,:c,:v)
                    ON CONFLICT (figi,interval,ts) DO UPDATE
                    SET open=:o,high=:h,low=:l,close=:c,volume=:v
                """), {"figi": figi, "ts": c["ts"], "o": c["open"], "h": c["high"],
                        "l": c["low"], "c": c["close"], "v": c["volume"]})
    return len(candles)


def sync_moex_daily(figi: str, ticker: str, days: int = 400) -> int:
    """Дневные свечи (interval=24) из MOEX ISS → candles(interval=24)."""
    now = datetime.now(timezone.utc)
    from_ = now - timedelta(days=max(30, int(days)))
    candles = moex_candles(ticker, from_.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"),
                           interval=24)
    if not candles:
        return 0
    engine = _sync_engine()
    with engine.begin() as db:
        for c in candles:
            db.execute(text("""
                INSERT INTO candles (figi,interval,ts,open,high,low,close,volume)
                VALUES (:figi,24,:ts,:o,:h,:l,:c,:v)
                ON CONFLICT (figi,interval,ts) DO UPDATE
                SET open=:o,high=:h,low=:l,close=:c,volume=:v
            """), {"figi": figi, "ts": c["ts"], "o": c["open"], "h": c["high"],
                    "l": c["low"], "c": c["close"], "v": c["volume"]})
    return len(candles)


async def ensure_moex_candles(figi: str, ticker: str, days: int = 10) -> int:
    """Прогнать MOEX ISS синк, только если в БД нет свежих 1м свечей за 2 дня."""
    import asyncio

    def _fresh():
        engine = _sync_engine()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with engine.connect() as db:
            n = db.execute(text(
                "SELECT count(*) FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:c"
            ), {"f": figi, "c": cutoff}).scalar()
        return bool(n and int(n) > 0)

    if await asyncio.to_thread(_fresh):
        return 0
    try:
        return await asyncio.to_thread(sync_moex_sync, figi, ticker, days)
    except Exception as e:
        print(f"[moex] ensure_moex_candles {ticker} failed: {e}")
        return 0
