import importlib.util
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import TransferManifestItem
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.services.transfer_omissions import normalize_source_file_error_policy
from app.settings import Settings


def test_omission_enums_expose_only_supported_values():
    assert importlib.util.find_spec("app.services.transfer_omissions") is not None
    from app.services.transfer_omissions import OmissionKind, SourceFileErrorPolicy

    assert {item.value for item in OmissionKind} == {
        "USER_ACCEPTED_SOURCE_FAILURE",
        "MEGA_FILE_RESTRICTED",
    }
    assert {item.value for item in SourceFileErrorPolicy} == {
        "STOP",
        "SKIP_CONFIRMED_RESTRICTED",
    }
    with pytest.raises(ValueError):
        normalize_source_file_error_policy("IGNORE_EVERYTHING")


@pytest.mark.asyncio
async def test_memory_store_round_trips_omission_fields_and_safe_defaults():
    store = MemoryStore()
    user = store.add_user("omission-owner", "omission@example.test", "hash")
    source = store.create_connection("Source", "MEGA", user.id)
    destination = store.create_connection("Destination", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/source"], "/dest")
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/file.rar",
                "destination_path": "/dest/file.rar",
                "size_bytes": 42,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]

    assert transfer.source_file_error_policy == "STOP"
    assert transfer.accepted_parent_omissions_attempt is None
    opted_in = store.create_transfer(
        user.id,
        source.id,
        destination.id,
        ["/opt-in"],
        "/dest",
        source_file_error_policy="SKIP_CONFIRMED_RESTRICTED",
    )
    assert opted_in.source_file_error_policy == "SKIP_CONFIRMED_RESTRICTED"
    assert opted_in.public_dict()["source_file_error_policy"] == "SKIP_CONFIRMED_RESTRICTED"
    assert item.omission_kind is None
    assert item.omitted_at is None
    assert item.omitted_by is None
    assert item.last_failure_stage is None
    assert item.last_failure_attempt is None

    omitted_at = datetime.now(UTC)
    item.status = "SKIPPED"
    item.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    item.omitted_at = omitted_at
    item.omitted_by = user.id
    item.last_failure_stage = "SOURCE_DOWNLOAD"
    item.last_failure_attempt = 2
    restored = (
        await store.alist_transfer_manifest_items(transfer.id, statuses={"SKIPPED"})
    )[0]
    assert restored.omission_kind == "USER_ACCEPTED_SOURCE_FAILURE"
    assert restored.omitted_at == omitted_at
    assert restored.omitted_by == user.id
    assert restored.last_failure_stage == "SOURCE_DOWNLOAD"
    assert restored.last_failure_attempt == 2
    summary = (await store.alist_transfer_manifest_summaries([transfer.id]))[transfer.id]
    assert summary["skipped"] == 1
    assert summary["omitted"] == 1
    assert summary["omitted_bytes"] == 42


@pytest.mark.asyncio
async def test_database_store_round_trips_omission_fields_and_safe_defaults(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'omissions.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("omission-owner", "omission@example.test", "hash")
    source = await store.acreate_connection("Source", "MEGA", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection(
        "Destination", "SEAFILE", user.id, uuid.uuid4(), b""
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/dest"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/file.rar",
                "destination_path": "/dest/file.rar",
                "size_bytes": 42,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    assert transfer.source_file_error_policy == "STOP"
    assert transfer.accepted_parent_omissions_attempt is None
    opted_in = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/opt-in"],
        "/dest",
        source_file_error_policy="SKIP_CONFIRMED_RESTRICTED",
    )
    persisted_opt_in = await store.aget_transfer(opted_in.id)
    assert persisted_opt_in is not None
    assert persisted_opt_in.source_file_error_policy == "SKIP_CONFIRMED_RESTRICTED"
    assert item.omission_kind is None
    assert item.omitted_at is None
    assert item.omitted_by is None
    assert item.last_failure_stage is None
    assert item.last_failure_attempt is None

    omitted_at = datetime.now(UTC)
    async with store.sessions() as session:
        row = await session.scalar(
            select(TransferManifestItem).where(TransferManifestItem.id == item.id)
        )
        assert row is not None
        row.status = "SKIPPED"
        row.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
        row.omitted_at = omitted_at
        row.omitted_by = user.id
        row.last_failure_stage = "SOURCE_DOWNLOAD"
        row.last_failure_attempt = 2
        await session.commit()

    restored = (
        await store.alist_transfer_manifest_items(transfer.id, statuses={"SKIPPED"})
    )[0]
    assert restored.omission_kind == "USER_ACCEPTED_SOURCE_FAILURE"
    assert restored.omitted_at == omitted_at.replace(tzinfo=None)
    assert restored.omitted_by == user.id
    assert restored.last_failure_stage == "SOURCE_DOWNLOAD"
    assert restored.last_failure_attempt == 2
    summary = (await store.alist_transfer_manifest_summaries([transfer.id]))[transfer.id]
    assert summary["skipped"] == 1
    assert summary["omitted"] == 1
    assert summary["omitted_bytes"] == 42
    await store.aclose()
