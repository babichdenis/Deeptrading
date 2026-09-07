import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from collections import defaultdict
from sqlalchemy import text
from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

async def scan(ticker):
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi FROM instruments WHERE ticker=:t"), {"t": ticker})).first()
        if not row: return {}
        c = await _lc(db, row.figi, 1, date_from=datetime(2026,8,1,tzinfo=timezone.utc), date_to=datetime(2026,9,1,tzinfo=timezone.utc))
    days = defaultdict(list)
    for x in c:
        d = x.ts.astimezone(MSK).date().isoformat()
        days[d].append(x)
    bad_days = []
    for d, xs in days.items():
        jumps = 0
        prev = None
        for x in xs:
            if prev:
                j = abs(x.close - prev) / prev if prev else 0
                if j > 0.35: jumps += 1
            prev = x.close
        if jumps >= 3:
            bad_days.append((d, jumps, len(xs)))
    return bad_days

async def main():
    tickers = ["AFLT","ASTR","CHMF","GAZP","GMKN","LENT","LKOH","MAGN","MTSS","MVID",
               "NLMK","NVTK","ROSN","RUAL","SBER","SFIN","SMLT","SNGSP","T","TRNFP"]
    for tk in tickers:
        bd = await scan(tk)
        if bd:
            print(f"{tk}: {len(bd)} плохих дней ->", bd[:5])

asyncio.run(main())
