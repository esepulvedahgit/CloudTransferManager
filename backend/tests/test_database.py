import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, User


@pytest.mark.asyncio
async def test_user_records_use_uuid_primary_keys():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        user = User(
            username="operator",
            email="operator@example.test",
            password_hash="not-a-real-password",
            role="USER",
        )
        session.add(user)
        await session.commit()
        result = await session.scalar(select(User).where(User.username == "operator"))

    assert result is not None
    assert isinstance(result.id, uuid.UUID)
    await engine.dispose()
