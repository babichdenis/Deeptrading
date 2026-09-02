import sys; sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
import asyncio, asyncpg
from app.config import get_settings
async def f():
    conn=await asyncpg.connect(get_settings().database_url.replace("+asyncpg",""))
    cols=await conn.fetch("select column_name,data_type from information_schema.columns where table_name='instruments' order by ordinal_position")
    print("INSTRUMENTS COLUMNS:", [(c['column_name'],c['data_type']) for c in cols])
    n=await conn.fetchval("select count(*) from instruments")
    print("instruments count:", n)
    samp=await conn.fetch("select * from instruments limit 3")
    for s in samp: print("SAMPLE:", dict(s))
    for kw in ['BRENT','BRN','OIL','GOLD','USD','ROSN','SBER','GAZP','LKOH','MOEX']:
        r=await conn.fetch(f"select figi, ticker, name, class_code, currency from instruments where figi ilike '%{kw}%' or ticker ilike '%{kw}%' or name ilike '%{kw}%' limit 4")
        if r: print(kw, [dict(x) for x in r])
    have=await conn.fetchval("select count(distinct figi) from candles")
    print("distinct figi WITH candles:", have)
    # how many instruments have candles
    both=await conn.fetchval("select count(*) from instruments i where exists(select 1 from candles c where c.figi=i.figi)")
    print("instruments having candles:", both)
    await conn.close()
asyncio.run(f())
