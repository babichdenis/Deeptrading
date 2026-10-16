#!/usr/bin/env python3
"""
fetch_equity_day.py — докачка day candles для российских акций.

Ключ: использовать BBG FIGIs из shares() API, НЕ TCS FIGIs из instrument_info.
TCS FIGIs (формат брокера) не дают историю через get_candles.

Usage:
    cd backend && .venv/bin/python3 scripts/fetch_equity_day.py
"""
import asyncio
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

from app.config import get_settings
from app.database import SessionLocal
from sqlalchemy import text
from t_tech.invest import CandleInterval
from t_tech.invest.clients import Client
from t_tech.invest.utils import quotation_to_decimal

NOW = datetime.now(timezone.utc)
FETCH_FROM = NOW - timedelta(days=400)


def q2f(q) -> float:
    return float(q.units + q.nano / 1e9)


def _candles(client, figi: str, from_: datetime, to: datetime) -> list[dict]:
    rows = {}
    cursor = from_
    while cursor < to:
        chunk_end = min(cursor + timedelta(days=30), to)
        resp = client.market_data.get_candles(
            figi=figi, interval=CandleInterval.CANDLE_INTERVAL_DAY,
            from_=cursor, to=chunk_end)
        for c in resp.candles:
            key = (figi, str(c.time))
            rows[key] = {
                "figi": figi, "interval": 5,
                "ts": c.time,
                "open":  Decimal(str(q2f(c.open))),
                "high":  Decimal(str(q2f(c.high))),
                "low":   Decimal(str(q2f(c.low))),
                "close": Decimal(str(q2f(c.close))),
                "volume": c.volume,
            }
        cursor = chunk_end
    return sorted(rows.values(), key=lambda r: r["ts"])


async def check_existing(figi: str) -> int:
    try:
        async with SessionLocal() as session:
            r = await session.execute(
                text("SELECT COUNT(*) FROM candles WHERE figi=:f AND interval=5"),
                {"f": figi})
            return r.scalar() or 0
    except Exception:
        return 0


async def upsert_candles(rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = text("""
        INSERT INTO candles (figi, interval, ts, open, high, low, close, volume)
        VALUES (:figi, :interval, :ts, :open, :high, :low, :close, :volume)
        ON CONFLICT (figi, interval, ts) DO UPDATE SET
            open=EXCLUDED.open, high=EXCLUDED.high,
            low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume
    """)
    try:
        async with SessionLocal() as session:
            await session.execute(sql, rows)
            await session.commit()
        return len(rows)
    except Exception as e:
        print(f"    candles error: {e}")
        return 0


async def upsert_instrument(figi: str, ticker: str, name: str,
                           sectype: str = "1", currency: str = "RUB") -> None:
    upd = text("UPDATE instrument_info SET name=:name, ticker=:ticker WHERE figi=:figi")
    ins = text("""
        INSERT INTO instrument_info
            (ticker, figi, name, sectype, currency, lot, decimals,
             api_trade_available, limit_order_available, market_order_available,
             bestprice_order_available, is_traded, updated_at)
        VALUES (:ticker, :figi, :name, :sectype, :currency, 1, 2,
                True, True, True, True, True, NOW())
        ON CONFLICT DO NOTHING
    """)
    try:
        async with SessionLocal() as session:
            r = await session.execute(upd, {"figi": figi, "name": name, "ticker": ticker})
            if r.rowcount == 0:
                await session.execute(ins, {"ticker": ticker, "figi": figi,
                                             "name": name, "sectype": sectype,
                                             "currency": currency})
            await session.commit()
    except Exception as e:
        print(f"    instrument error: {e}")


async def main():
    settings = get_settings()
    token = settings.tinkoff_token
    print(f"=== Equity Day Candles Fetch ===")
    print(f"Window: {FETCH_FROM.date()} → {NOW.date()}\n")

    # Получить BBG FIGIs из shares() API
    print("[Resolving BBG FIGIs from shares() API]")

    def _get_equity_figis():
        with Client(token=token) as svc:
            r = svc.instruments.shares()
            priority = ['SBER', 'GAZP', 'VTBR', 'ROSN', 'LKOH', 'NVTK', 'SMLT',
                        'MGNM', 'SELG', 'MGNT', 'SFIN', 'ALRS', 'VKCO', 'OZON',
                        'YDEX', 'AFLT', 'CHMF', 'NLMK', 'GMKN', 'RUAL',
                        'PIKK', 'DSKY', 'BELU', 'SBERP']
            found = {}
            for x in r.instruments:
                ticker = getattr(x, 'ticker', '')
                if ticker in priority and getattr(x, 'figi', '').startswith('BBG'):
                    figi = getattr(x, 'figi', '')
                    if figi and ticker not in found:
                        found[ticker] = (figi, x.name)
            return sorted(found.items())

    found_items = await asyncio.to_thread(_get_equity_figis)
    equity_targets = []
    for ticker, (figi, name) in found_items:
        existing = await check_existing(figi)
        equity_targets.append((ticker, figi, name, existing))
        status = f"has {existing} day candles" if existing > 0 else "need fetch"
        print(f"  {ticker}: {figi} — {status}")

    if not equity_targets:
        print("No equity FIGIs found!")
        return

    print(f"\n[Fetching day candles]")
    total = 0
    for ticker, figi, name, existing in equity_targets:
        if existing > 300:
            print(f"  {ticker}: skip ({existing} rows)")
            continue
        await upsert_instrument(figi, ticker, name)
        print(f"  {ticker} ({figi}): fetching...")

        def _fetch():
            with Client(token=token) as svc:
                return _candles(svc, figi, FETCH_FROM, NOW)

        try:
            rows = await asyncio.to_thread(_fetch)
            if not rows:
                print(f"    {ticker}: 0 rows")
                continue
            n = await upsert_candles(rows)
            total += n
            print(f"    {ticker}: {len(rows)} candles, {n} upserted")
        except Exception as e:
            print(f"    {ticker}: FAIL — {e}")

    print(f"\n=== Done: {total} rows upserted ===")


if __name__ == "__main__":
    asyncio.run(main())
