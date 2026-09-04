"""Точечное восполнение 1m-свечей в БД для набора инструментов.

MOEX ISS (параллельно) -> fallback T-Invest -> upsert в таблицу candles.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

MSK = ZoneInfo("Europe/Moscow")

_UP = (
    "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
    "VALUES (:f, 1, :ts, :o, :h, :l, :c, :v) "
    "ON CONFLICT (figi, interval, ts) DO UPDATE SET "
    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
    "close=EXCLUDED.close, volume=EXCLUDED.volume"
)


def _trading_days(want_from: datetime, want_to: datetime) -> list[datetime]:
    days = []
    cur = datetime(want_from.year, want_from.month, want_from.day, tzinfo=MSK)
    end = datetime(want_to.year, want_to.month, want_to.day, tzinfo=MSK)
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _engine():
    from sqlalchemy import create_engine
    from app.config import get_settings

    s = get_settings()
    url = (f"postgresql://{s.postgres_user}:{s.postgres_password}"
           f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}")
    return create_engine(url, pool_size=8, max_overflow=8)


def _moex_candles(ticker: str, d: str) -> list[dict]:
    url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
           f"/securities/{ticker}/candles.json?from={d}&till={d}&interval=1")
    data = json.loads(urllib.request.urlopen(url, timeout=20).read())
    cols = {n: i for i, n in enumerate(data["candles"]["columns"])}
    out = []
    for r in data["candles"]["data"]:
        ts = r[cols["begin"]]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        out.append({
            "open": r[cols["open"]], "high": r[cols["high"]],
            "low": r[cols["low"]], "close": r[cols["close"]],
            "volume": r[cols["volume"]], "ts": ts,
        })
    return out


def _upsert_rows(engine, figi: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    n = 0
    with engine.begin() as db:
        for c in rows:
            db.execute(text(_UP), {
                "f": figi, "ts": c["ts"], "o": c["open"], "h": c["high"],
                "l": c["low"], "c": c["close"], "v": int(c["volume"] or 0),
            })
            n += 1
    return n


def _backfill_day_moex(engine, figi: str, ticker: str, day_msk: datetime) -> int:
    try:
        rows = _moex_candles(ticker, day_msk.strftime("%Y-%m-%d"))
    except Exception:
        rows = []
    return _upsert_rows(engine, figi, rows)


def _backfill_day_tinvest(engine, figi: str, day_msk: datetime) -> int:
    from app.services.tinvest import CandleInterval, fetch_candles
    from app.services.candle_cache import upsert_candles
    from app.database import SessionLocal

    day_start_utc = day_msk.astimezone(timezone.utc)
    day_end_utc = (day_msk + timedelta(days=1)).astimezone(timezone.utc)

    async def _run():
        rows = await asyncio.to_thread(
            fetch_candles, figi, CandleInterval.CANDLE_INTERVAL_1_MIN,
            day_start_utc, day_end_utc,
        )
        if not rows:
            return 0
        async with SessionLocal() as db:
            return await upsert_candles(db, rows)

    try:
        return asyncio.run(_run())
    except Exception:
        return 0


def _existing_days(engine, figi: str, want_from: datetime, want_to: datetime) -> set[datetime]:
    with engine.connect() as db:
        res = db.execute(text(
            "SELECT DISTINCT (ts AT TIME ZONE 'Europe/Moscow')::date FROM candles "
            "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<=:b"
        ), {"f": figi, "a": want_from, "b": want_to}).fetchall()
    return {datetime(r[0].year, r[0].month, r[0].day, tzinfo=MSK) for r in res}


async def run_backfill(
    instruments: list[tuple[str, str]],
    days: int = 35,
    workers: int = 4,
    log=print,
) -> dict:
    engine = _engine()
    now = datetime.now(timezone.utc)
    want_from = now - timedelta(days=days)
    want_to = now
    trading_days = _trading_days(want_from, want_to)

    report = {"instruments": len(instruments), "days_expected": len(trading_days)}
    total_added = 0
    for figi, ticker in instruments:
        try:
            have = _existing_days(engine, figi, want_from, want_to)
        except Exception as e:
            report.setdefault("errors", []).append(f"{figi}: coverage {e}")
            continue
        missing = [d for d in trading_days if d not in have]
        if not missing:
            report.setdefault("complete", []).append(figi)
            continue
        log(f"[{figi}] {ticker or '?'} missing {len(missing)} days: "
            f"{missing[0]:%d.%m}..{missing[-1]:%d.%m}")
        added_instr = 0
        sources = {"moex": 0, "tinvest": 0, "empty": 0}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
            moex_added = list(pool.map(lambda d: _backfill_day_moex(engine, figi, ticker, d), missing))
        for day, ma in zip(missing, moex_added):
            if ma > 0:
                sources["moex"] += 1
                added_instr += ma
                continue
            ta = _backfill_day_tinvest(engine, figi, day)
            if ta > 0:
                sources["tinvest"] += 1
                added_instr += ta
            else:
                sources["empty"] += 1
                log(f"    {day:%d.%m} — пусто в обоих источниках")
        report.setdefault("by_figi", {})[figi] = {
            "missing_days": len(missing), "added": added_instr, "sources": sources,
        }
        total_added += added_instr
        log(f"    added {added_instr} bars")
    report["total_added"] = total_added
    return report
