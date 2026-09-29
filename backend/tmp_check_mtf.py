import sys, json, asyncio
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal

d = json.load(sys.stdin)
fs = [x["figi"] for x in (d.get("universe") or []) if x.get("figi")]

async def main():
    cut = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi, count(*) FROM candles WHERE interval = 5 "
            "AND figi = ANY(:fs) AND ts <= :cut "
            "AND ts > :cut - interval '20 days' GROUP BY figi"
        ), {"fs": fs, "cut": cut})).all()
    print("figi with 5m data in window:", len(rows), "of", len(fs))
    have = {r[0] for r in rows}
    missing = sorted(set(fs) - have)
    print("missing:", missing[:10], "..." if len(missing) > 10 else "")

asyncio.run(main())