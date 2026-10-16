#!/usr/bin/env python3
"""
arbitrage_data_fetch.py — докачка FX и фьючерсов в БД.

Шаг 1: получить реальные FIGI из currencies/futures API
Шаг 2: скачать candles (hour + day) за 1 год
Шаг 3: upsert в БД

Usage:
    cd backend && .venv/bin/python3 scripts/arbitrage_data_fetch.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
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
FETCH_FROM = NOW - timedelta(days=365)
CHUNK_DAYS = 30


def q2f(q) -> float:
    return float(q.units + q.nano / 1e9)


def _candles(client, figi: str, interval: CandleInterval,
             from_: datetime, to: datetime) -> list[dict]:
    rows = {}
    cursor = from_
    while cursor < to:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), to)
        resp = client.market_data.get_candles(
            figi=figi, interval=interval, from_=cursor, to=chunk_end)
        for c in resp.candles:
            key = (figi, int(interval.value), str(c.time))
            rows[key] = {
                "figi": figi,
                "interval": int(interval.value),
                "ts": c.time,
                "open":  Decimal(str(q2f(c.open))),
                "high":  Decimal(str(q2f(c.high))),
                "low":   Decimal(str(q2f(c.low))),
                "close": Decimal(str(q2f(c.close))),
                "volume": c.volume,
            }
        cursor = chunk_end
    return sorted(rows.values(), key=lambda r: r["ts"])


async def ensure_instrument(figi: str, ticker: str, name: str,
                            sectype: str, currency: str = "RUB") -> bool:
    upd = text("""
        UPDATE instrument_info SET
            name = :name, ticker = :ticker, updated_at = NOW()
        WHERE figi = :figi
    """)
    ins = text("""
        INSERT INTO instrument_info
            (ticker, figi, name, sectype, currency,
             lot, decimals, api_trade_available,
             limit_order_available, market_order_available,
             bestprice_order_available, is_traded, updated_at)
        VALUES
            (:ticker, :figi, :name, :sectype, :currency,
             1, 2, True, True, True, True, True, NOW())
        ON CONFLICT DO NOTHING
    """)
    try:
        async with SessionLocal() as session:
            r = await session.execute(upd, {"figi": figi, "name": name, "ticker": ticker})
            if r.rowcount == 0:
                await session.execute(ins, {"ticker": ticker, "figi": figi, "name": name,
                                            "sectype": sectype, "currency": currency})
            await session.commit()
        return True
    except Exception as e:
        print(f"    instrument error {figi}: {e}")
        return False


async def check_existing(figi: str, interval: int) -> int:
    try:
        async with SessionLocal() as session:
            r = await session.execute(
                text("SELECT COUNT(*) FROM candles WHERE figi=:f AND interval=:i"),
                {"f": figi, "i": interval}
            )
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


async def download_and_save(client, ticker: str, figi: str, name: str,
                            intervals: list, sectype: str = "FX") -> int:
    await ensure_instrument(figi, ticker, name, sectype)
    total = 0
    for intv_obj in intervals:
        iv = int(intv_obj.value)
        existing = await check_existing(figi, iv)
        if existing > 300:
            print(f"  {ticker}: interval={iv} already {existing} rows, skip")
            continue
        try:
            rows = _candles(client, figi, intv_obj, FETCH_FROM, NOW)
            if not rows:
                print(f"  {ticker}: interval={iv} → 0 rows")
                continue
            n = await upsert_candles(rows)
            total += n
            print(f"  {ticker}: interval={iv} → {len(rows)} candles, {n} upserted")
        except Exception as e:
            print(f"  {ticker}: interval={iv} FAIL → {e}")
    return total


async def main():
    settings = get_settings()
    token = settings.tinkoff_token
    print(f"=== Arbitrage Data Fetch ===")
    print(f"Token: {token[:12]}...")
    print(f"Window: {FETCH_FROM.date()} → {NOW.date()}")
    print(f"DB: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}\n")

    HOUR = CandleInterval.CANDLE_INTERVAL_HOUR
    DAY  = CandleInterval.CANDLE_INTERVAL_DAY

    with Client(token=token) as svc:
        # ── 1. Currencies — получить FIGI динамически ──────────────────
        print("[FX — resolving FIGIs]")
        fx_targets = []
        try:
            c_resp = svc.instruments.currencies()
            for x in c_resp.instruments:
                t = getattr(x, "ticker", "")
                f = getattr(x, "figi", "")
                uid = getattr(x, "uid", "")
                n = x.name
                if f:
                    # ключевые валюты
                    if any(k in t for k in ["USD", "EUR", "CNY", "GBP", "CHF", "TRY", "KZT", "JPY"]):
                        fx_targets.append((t, f, n, "FX", "RUB"))
            print(f"  Found {len(fx_targets)} currency instruments")
            for t, f, n, *_ in fx_targets[:5]:
                print(f"    {t}: {f} = {n}")
        except Exception as e:
            print(f"  currencies error: {e}")
            fx_targets = []

        # ── 2. Download FX ─────────────────────────────────────────────
        print("\n[FX candles]")
        fx_total = 0
        for ticker, figi, name, st, currency in fx_targets:
            n = await download_and_save(svc, ticker, figi, name, [HOUR, DAY], st)
            fx_total += n

        # ── 3. Futures — приоритетные ──────────────────────────────────
        print("\n[FUTURES — resolving FIGIs]")
        priority_futures = []
        try:
            f_resp = svc.instruments.futures()
            candidates = {}
            for x in f_resp.instruments:
                t = getattr(x, "ticker", "")
                f = getattr(x, "figi", "")
                n = x.name
                currency = getattr(x, "currency", "RUB")
                if t and f:
                    candidates[t] = (f, n, currency)

            # Ищем приоритетные
            priority_names = ["Si", "BR", "RTS", "MIX", "VTBX", "IMOEX", "GLDRUB"]
            for p in priority_names:
                for t, (f, n, c) in candidates.items():
                    if p.lower() in t.lower() or p.lower() in n.lower():
                        priority_futures.append((t, f, n, "FUT", c))
                        break
            print(f"  Found {len(priority_futures)} priority futures")
            for t, f, n, *_ in priority_futures:
                print(f"    {t}: {f} = {n}")
        except Exception as e:
            print(f"  futures error: {e}")

        print("\n[FUTURES candles]")
        fut_total = 0
        for ticker, figi, name, st, currency in priority_futures:
            n = await download_and_save(svc, ticker, figi, name, [HOUR, DAY], st)
            fut_total += n

    print(f"\n=== Done ===")
    print(f"FX rows: {fx_total}")
    print(f"Futures rows: {fut_total}")


if __name__ == "__main__":
    asyncio.run(main())
