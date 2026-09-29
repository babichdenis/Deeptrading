import asyncio
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/Volumes/Dev/Deeptrading/backend")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.engine.candlehub import CandleHub

DB_URL = "postgresql+asyncpg://deeptrading:deeptrading@127.0.0.1:5432/deeptrading"


async def main():
    engine = create_async_engine(DB_URL)
    async with engine.connect() as conn:
        row = await conn.execute(text("""
            SELECT figi, COUNT(*) as n, MIN(ts) as min_ts, MAX(ts) as max_ts
            FROM candles
            WHERE interval = 1
            GROUP BY figi
            ORDER BY n DESC
            LIMIT 10
        """))
        print("=== 1min candles per figi ===")
        for r in row:
            print(f"  {r[0]:20s} n={r[1]:6d}  {r[2]} .. {r[3]}")

        row = await conn.execute(text("""
            SELECT figi, ts, open, high, low, close, volume
            FROM candles
            WHERE interval = 1 AND figi = 'BBG004730N88'
            ORDER BY ts DESC
            LIMIT 5
        """))
        print("\n=== last 5 SBER 1min candles ===")
        for r in row:
            print(f"  {r[1]}  O={r[2]:.2f} H={r[3]:.2f} L={r[4]:.2f} C={r[5]:.2f} V={r[6]}")

        row = await conn.execute(text("""
            SELECT ts, open, high, low, close, volume
            FROM candles
            WHERE interval = 1 AND figi = 'BBG004730N88'
            ORDER BY ts DESC
            LIMIT 500
        """))
        candles = list(row)
        print(f"\n=== loading {len(candles)} SBER 1min candles into CandleHub ===")

        hub = CandleHub()
        for ts, o, h, l, c, v in reversed(candles):
            from app.engine.models import Candle
            hub.ingest_1m("BBG004730N88", Candle(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v)), source="db")

        print(f"hub keys: {hub.keys()}")
        print(f"stats: {hub.stats()}")

        gaps = hub.gap_report("BBG004730N88")
        print(f"\n=== gap_report: {len(gaps)} gaps ===")
        for start, end in gaps[:20]:
            dur = (end - start).total_seconds() / 60 + 1
            print(f"  {start} .. {end}  ({int(dur)} min)")
        if len(gaps) > 20:
            print(f"  ... and {len(gaps) - 20} more")

    await engine.dispose()


asyncio.run(main())
