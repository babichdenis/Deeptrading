from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_s = get_settings()
engine = create_async_engine(_s.database_url, pool_pre_ping=True,
                             pool_size=int(getattr(_s, "db_pool_size", 10) or 10),
                             max_overflow=int(getattr(_s, "db_max_overflow", 20) or 20),
                             pool_timeout=60)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
