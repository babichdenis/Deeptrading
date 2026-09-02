import sys; sys.path.insert(0,"/Users/Denis/Dev/Deeptrading/backend")
import asyncio, asyncpg
from app.config import get_settings
async def f():
    c=await asyncpg.connect(get_settings().database_url.replace("+asyncpg",""))
    r=await c.fetch("select figi, count(*) n from candles group by figi order by n desc")
    print("CANDLES FIGI (count):")
    for x in r: print(x['figi'], x['n'])
    await c.close()
asyncio.run(f())
