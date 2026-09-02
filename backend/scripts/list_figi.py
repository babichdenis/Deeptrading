import sys; sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")
import asyncio, asyncpg
from app.config import get_settings
async def f():
    conn=await asyncpg.connect(get_settings().database_url)
    rows=await conn.fetch("select distinct figi from candle order by figi")
    print("N=",len(rows))
    for r in rows: print(r["figi"])
    await conn.close()
asyncio.run(f())
