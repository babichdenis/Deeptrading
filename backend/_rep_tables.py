import asyncio

from sqlalchemy import text

from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        names = (await db.execute(text(
            "select tablename from pg_tables where schemaname = 'public' order by 1"
        ))).scalars().all()
    print(", ".join(names))


asyncio.run(main())
