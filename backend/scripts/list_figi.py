import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import asyncio, asyncpg
from app.config import get_settings
async def f():
    conn=await asyncpg.connect(get_settings().database_url)
    rows=await conn.fetch("select distinct figi from candle order by figi")
    print("N=",len(rows))
    for r in rows: print(r["figi"])
    await conn.close()
asyncio.run(f())
