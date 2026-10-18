import asyncio
import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, TransferEvent, TransferJob, TransferManifestItem
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.services.transfer_omissions import (
    TransferRecoveryConflictError,
    TransferRecoveryNotFoundError,
    normalize_source_file_error_policy,
)
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

    await store.aupdate_transfer_manifest_item(
        transfer.id,
        item.id,
        status="FAILED",
        error_code="MEGACMD_COMMAND_FAILED",
        failure_stage="SOURCE_DOWNLOAD",
        failure_attempt=3,
    )
    failed_item = (await store.alist_transfer_manifest_items(transfer.id, statuses={"FAILED"}))[0]
    assert failed_item.last_failure_stage == "SOURCE_DOWNLOAD"
    assert failed_item.last_failure_attempt == 3

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

    await store.aupdate_transfer_manifest_item(
        transfer.id,
        item.id,
        status="FAILED",
        error_code="MEGACMD_COMMAND_FAILED",
        failure_stage="SOURCE_DOWNLOAD",
        failure_attempt=3,
    )
    failed_item = (await store.alist_transfer_manifest_items(transfer.id, statuses={"FAILED"}))[0]
    assert failed_item.last_failure_stage == "SOURCE_DOWNLOAD"
    assert failed_item.last_failure_attempt == 3

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


@pytest.mark.asyncio
async def test_database_store_skip_and_resume_commits_manifest_and_audit_together(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'recovery.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("recovery-owner", "recovery@example.test", "hash")
    source = await store.acreate_connection("MEGA", "MEGA", user.id, uuid.uuid4(), b"source")
    destination = await store.acreate_connection(
        "Seafile", "SEAFILE", user.id, uuid.uuid4(), b"destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 686_289_042,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    started_at = datetime.now(UTC).replace(microsecond=0)
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        manifest_row = await session.get(TransferManifestItem, item.id)
        assert row is not None and manifest_row is not None
        row.status = "FAILED"
        row.attempt_count = 2
        row.started_at = started_at
        row.completed_at = started_at
        row.files_total = 1
        row.files_completed = 1
        row.error_count = 1
        row.warning_count = 1
        manifest_row.status = "FAILED"
        manifest_row.error_code = "MEGACMD_COMMAND_FAILED"
        session.add(
            TransferEvent(
                transfer_job_id=transfer.id,
                level="ERROR",
                event_type="FAILURE_DIAGNOSTIC",
                message="Source file copy failed",
                technical_details={
                    "file_name": "restricted.rar",
                    "provider": "MEGA",
                    "provider_stage": "SOURCE_DOWNLOAD",
                    "destination_write_started": False,
                },
                provider_error_code="MEGACMD_COMMAND_FAILED",
                created_at=started_at + timedelta(seconds=1),
            )
        )
        await session.commit()

    request_id = uuid.uuid4()
    def fail_audit_flush(session, _flush_context, _instances):
        if any(
            isinstance(pending, AuditEvent) and pending.action == "TRANSFER_FILE_OMITTED"
            for pending in session.new
        ):
            raise RuntimeError("audit storage unavailable")

    event.listen(Session, "before_flush", fail_audit_flush)
    try:
        with pytest.raises(RuntimeError, match="audit storage unavailable"):
            await store.askip_transfer_item_and_resume(
                transfer.id,
                item.id,
                actor_id=user.id,
                expected_attempt=2,
                request_id=request_id,
            )
    finally:
        event.remove(Session, "before_flush", fail_audit_flush)
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        manifest_row = await session.get(TransferManifestItem, item.id)
        audit_count = int(await session.scalar(select(func.count(AuditEvent.id))) or 0)
        assert row is not None and row.status == "FAILED" and row.attempt_count == 2
        assert manifest_row is not None and manifest_row.status == "FAILED"
        assert audit_count == 0

    resumed = await store.askip_transfer_item_and_resume(
        transfer.id,
        item.id,
        actor_id=user.id,
        expected_attempt=2,
        request_id=request_id,
    )
    replay = await store.askip_transfer_item_and_resume(
        transfer.id,
        item.id,
        actor_id=user.id,
        expected_attempt=2,
        request_id=request_id,
    )
    with pytest.raises(TransferRecoveryConflictError):
        await store.askip_transfer_item_and_resume(
            transfer.id,
            uuid.uuid4(),
            actor_id=user.id,
            expected_attempt=2,
            request_id=request_id,
        )
    async with store.sessions() as session:
        audit_actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action)
                    .where(AuditEvent.resource_id == str(transfer.id))
                    .order_by(AuditEvent.action)
                )
            ).all()
        )
        manifest_row = await session.get(TransferManifestItem, item.id)
        assert manifest_row is not None
        assert manifest_row.status == "SKIPPED"
        assert manifest_row.omission_kind == "USER_ACCEPTED_SOURCE_FAILURE"
        assert manifest_row.omitted_by == user.id
    await store.aclose()

    assert resumed.status == "QUEUED" and resumed.attempt_count == 3
    assert replay.idempotent_replay is True
    assert replay.attempt_count == 3
    assert audit_actions == ["TRANSFER_FILE_OMITTED", "TRANSFER_RESUMED"]


@pytest.mark.asyncio
async def test_database_store_rejects_active_reservation_and_credential_changes(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'unsafe-recovery.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("unsafe-owner", "unsafe@example.test", "hash")
    source = await store.acreate_connection("MEGA", "MEGA", user.id, uuid.uuid4(), b"source")
    destination = await store.acreate_connection(
        "Seafile", "SEAFILE", user.id, uuid.uuid4(), b"destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/restricted.rar",
                "destination_path": "/destination/restricted.rar",
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    started_at = datetime.now(UTC)
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        manifest_row = await session.get(TransferManifestItem, item.id)
        assert row is not None and manifest_row is not None
        row.status = "FAILED"
        row.attempt_count = 2
        row.started_at = started_at
        manifest_row.status = "FAILED"
        manifest_row.error_code = "MEGACMD_COMMAND_FAILED"
        session.add(
            TransferEvent(
                transfer_job_id=transfer.id,
                level="ERROR",
                event_type="FAILURE_DIAGNOSTIC",
                message="Source file copy failed",
                technical_details={
                    "file_name": "restricted.rar",
                    "provider": "MEGA",
                    "provider_stage": "SOURCE_DOWNLOAD",
                    "destination_write_started": False,
                },
                provider_error_code="MEGACMD_COMMAND_FAILED",
            )
        )
        row.dispatch_token = uuid.uuid4()
        await session.commit()

    with pytest.raises(TransferRecoveryConflictError, match="execution is still active"):
        await store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=user.id,
            expected_attempt=2,
            request_id=uuid.uuid4(),
        )
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        assert row is not None
        row.dispatch_token = None
        await session.commit()

    connection = await store.aget_connection(source.id)
    assert connection is not None
    connection.encrypted_credentials = b"rotated"
    await store.asave_connection(connection)
    with pytest.raises(TransferRecoveryConflictError, match="credentials changed"):
        await store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=user.id,
            expected_attempt=2,
            request_id=uuid.uuid4(),
        )
    await store.aclose()


@pytest.mark.asyncio
async def test_memory_store_fences_concurrent_replays_and_revoked_connection_access():
    store = MemoryStore()
    user = store.add_user("private-owner", "private@example.test", "hash")
    source = store.create_connection("MEGA", "MEGA", user.id, is_global=False)
    destination = store.create_connection("Seafile", "SEAFILE", user.id, is_global=False)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/source"], "/dest")
    transfer.status = "FAILED"
    transfer.attempt_count = 2
    transfer.started_at = datetime.now(UTC) - timedelta(minutes=1)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [{"source_path": "/source/file.rar", "destination_path": "/dest/file.rar"}],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    item.status = "FAILED"
    item.error_code = "MEGACMD_COMMAND_FAILED"
    await store.aadd_transfer_event(
        transfer.id,
        level="ERROR",
        event_type="FAILURE_DIAGNOSTIC",
        message="Source file copy failed",
        provider_error_code="MEGACMD_COMMAND_FAILED",
        technical_details={
            "file_name": "file.rar",
            "provider": "MEGA",
            "provider_stage": "SOURCE_DOWNLOAD",
            "destination_write_started": False,
        },
    )
    with pytest.raises(TransferRecoveryNotFoundError, match="Transfer not found"):
        await store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=user.id,
            expected_attempt=2,
            request_id=uuid.uuid4(),
        )

    await store.agrant_connection(user.id, source.id, user.id)
    await store.agrant_connection(user.id, destination.id, user.id)
    request_id = uuid.uuid4()
    first, second = await asyncio.gather(
        store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=user.id,
            expected_attempt=2,
            request_id=request_id,
        ),
        store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=user.id,
            expected_attempt=2,
            request_id=request_id,
        ),
    )

    assert first.status == second.status == "QUEUED"
    assert first.attempt_count == second.attempt_count == 3
    assert (
        len(
            [
                event
                for event in store.audit_events
                if event["action"] == "TRANSFER_FILE_OMITTED"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_postgres_skip_and_resume_serializes_concurrent_same_request():
    database_url = os.environ.get("CTM_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("CTM_TEST_POSTGRES_URL is not configured")
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    user = await store.acreate_user("pg-recovery", "pg-recovery@example.test", "hash")
    source = await store.acreate_connection("MEGA", "MEGA", user.id, uuid.uuid4(), b"source")
    destination = await store.acreate_connection(
        "Seafile", "SEAFILE", user.id, uuid.uuid4(), b"destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 686_289_042,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    started_at = datetime.now(UTC)
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        manifest_row = await session.get(TransferManifestItem, item.id)
        assert row is not None and manifest_row is not None
        row.status = "FAILED"
        row.attempt_count = 2
        row.started_at = started_at
        manifest_row.status = "FAILED"
        manifest_row.error_code = "MEGACMD_COMMAND_FAILED"
        session.add(
            TransferEvent(
                transfer_job_id=transfer.id,
                level="ERROR",
                event_type="FAILURE_DIAGNOSTIC",
                message="Source file copy failed",
                technical_details={
                    "file_name": "restricted.rar",
                    "provider": "MEGA",
                    "provider_stage": "SOURCE_DOWNLOAD",
                    "destination_write_started": False,
                },
                provider_error_code="MEGACMD_COMMAND_FAILED",
            )
        )
        await session.commit()

    request_id = uuid.uuid4()
    results = await asyncio.gather(
        *[
            store.askip_transfer_item_and_resume(
                transfer.id,
                item.id,
                actor_id=user.id,
                expected_attempt=2,
                request_id=request_id,
            )
            for _ in range(2)
        ]
    )
    async with store.sessions() as session:
        attempt_count = int(
            await session.scalar(
                select(TransferJob.attempt_count).where(TransferJob.id == transfer.id)
            )
            or 0
        )
        audit_count = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == str(transfer.id),
                    AuditEvent.action == "TRANSFER_FILE_OMITTED",
                )
            )
            or 0
        )
    await store.aclose()

    assert attempt_count == 3
    assert audit_count == 1
    assert all(result.status == "QUEUED" and result.attempt_count == 3 for result in results)
