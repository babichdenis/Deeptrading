import sys; sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
import asyncio, asyncpg
from app.config import get_settings
async def f():
    conn=await asyncpg.connect(get_settings().database_url.replace("+asyncpg",""))
    tabs=await conn.fetch("select table_name from information_schema.tables where table_schema='public' order by table_name")
    print("TABLES:", [t['table_name'] for t in tabs])
    for t in tabs:
        tn=t['table_name']
        try:
            n=await conn.fetchval(f"select count(*) from {tn}")
            print(f"  {tn}: {n}")
        except Exception as e:
            print(f"  {tn}: ERR {e}")
    rows=await conn.fetch("select distinct figi from candle order by figi")
    print("CANDLE FIGI COUNT:", len(rows))
    print([r['figi'] for r in rows])
    await conn.close()
asyncio.run(f())
