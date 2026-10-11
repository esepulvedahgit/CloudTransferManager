from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings import Settings, get_settings


def create_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    current = settings or get_settings()
    engine = create_async_engine(current.resolved_database_url(), pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    sessions = create_session_factory()
    async with sessions() as session:
        yield session
