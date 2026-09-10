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
        _ENGINE_CACHE = create_engine(url, pool_size=30, max_overflow=30)
    return _ENGINE_CACHE


def moex_candles(ticker: str, from_date: str, till_date: str) -> list[dict]:
    """1-мин свечи из свободного MOEX ISS (без T-Invest лимитов)."""
    url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
           f"/securities/{ticker}/candles.json?from={from_date}&till={till_date}&interval=1")
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
