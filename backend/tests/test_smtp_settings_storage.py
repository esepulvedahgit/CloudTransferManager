import uuid
from dataclasses import replace
from typing import Any

import pytest

import app.services.memory_store as memory_store_module
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.settings import Settings


def smtp_settings_record_type() -> Any:
    record_type = getattr(memory_store_module, "SMTPSettingsRecord", None)
    assert record_type is not None, "SMTPSettingsRecord must exist in the repository contract"
    return record_type


def new_record() -> Any:
    return smtp_settings_record_type()(
        id=uuid.uuid4(),
        host="smtp.example.test",
        port=587,
        use_tls=True,
        from_address="notifications@example.test",
        encrypted_credentials=b"\x00opaque-encrypted-credentials",
    )


@pytest.mark.asyncio
async def test_memory_store_persists_smtp_settings():
    record = new_record()
    store = MemoryStore()

    assert await store.aget_smtp_settings() is None
    created = await store.asave_smtp_settings(record)
    assert created == record
    assert await store.aget_smtp_settings() == record

    updated = replace(record, host="mail.example.test", port=465, use_tls=False)
    saved = await store.asave_smtp_settings(updated)
    loaded = await store.aget_smtp_settings()

    assert saved == updated
    assert loaded == updated
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.encrypted_credentials == b"\x00opaque-encrypted-credentials"


@pytest.mark.asyncio
async def test_database_store_persists_smtp_settings(tmp_path):
    record = new_record()
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'smtp.sqlite'}")
    )
    await store.create_schema()

    try:
        assert await store.aget_smtp_settings() is None
        created = await store.asave_smtp_settings(record)
        assert created == record
        assert await store.aget_smtp_settings() == record

        updated = replace(record, host="mail.example.test", port=465, use_tls=False)
        saved = await store.asave_smtp_settings(updated)
        loaded = await store.aget_smtp_settings()

        assert saved == updated
        assert loaded == updated
        assert loaded is not None
        assert loaded.id == record.id
        assert loaded.encrypted_credentials == b"\x00opaque-encrypted-credentials"
    finally:
        await store.aclose()
