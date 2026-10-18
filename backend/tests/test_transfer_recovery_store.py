import asyncio
import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    StorageConnection,
    TransferEvaluation,
    TransferEvent,
    TransferJob,
    TransferManifestItem,
    TransferNotificationOutbox,
    User,
)
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
    other_admin = await store.acreate_user(
        "recovery-other-admin", "recovery-other-admin@example.test", "hash", role="ADMIN"
    )
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
    with pytest.raises(TransferRecoveryConflictError, match="actor"):
        await store.askip_transfer_item_and_resume(
            transfer.id,
            item.id,
            actor_id=other_admin.id,
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
    manifest_counts = await store.aget_transfer_manifest_counts(transfer.id)
    async with store.sessions() as session:
        row = await session.get(TransferJob, transfer.id)
        assert row is not None
        row.status = "COMPLETED_WITH_WARNINGS"
        await session.commit()
    with pytest.raises(TransferRecoveryConflictError, match="Request id"):
        await store.aretry_omitted_transfer_items(
            transfer.id,
            item_ids=[item.id],
            actor_id=user.id,
            expected_attempt=3,
            request_id=request_id,
        )
    await store.aclose()

    assert resumed.status == "QUEUED" and resumed.attempt_count == 3
    assert replay.idempotent_replay is True
    assert replay.attempt_count == 3
    assert audit_actions == ["TRANSFER_FILE_OMITTED", "TRANSFER_RESUMED"]
    assert manifest_counts["omitted"] == 1
    assert manifest_counts["omitted_bytes"] == 686_289_042


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
async def test_database_accept_parent_omissions_is_attempt_scoped_and_idempotent(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'parent-acceptance.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("parent-accept-owner", "parent-accept@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/parent"], "/destination"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/child"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )
    await store.aadd_transfer_manifest_items(
        parent.id,
        [
            {
                "source_path": "/parent/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 100,
            }
        ],
    )
    async with store.sessions() as session:
        async with session.begin():
            parent_row = await session.get(TransferJob, parent.id)
            omitted_row = await session.scalar(
                select(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == parent.id
                )
            )
            assert parent_row is not None and omitted_row is not None
            parent_row.status = "COMPLETED_WITH_WARNINGS"
            omitted_row.status = "SKIPPED"
            omitted_row.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
            omitted_row.omitted_at = datetime.now(UTC)

    request_id = uuid.uuid4()
    accepted = await store.aaccept_parent_omissions(
        child.id,
        actor_id=user.id,
        expected_parent_attempt=parent.attempt_count,
        request_id=request_id,
    )
    replay = await store.aaccept_parent_omissions(
        child.id,
        actor_id=user.id,
        expected_parent_attempt=parent.attempt_count,
        request_id=request_id,
    )
    with pytest.raises(TransferRecoveryConflictError):
        await store.aaccept_parent_omissions(
            child.id,
            actor_id=user.id,
            expected_parent_attempt=parent.attempt_count + 1,
            request_id=uuid.uuid4(),
        )

    async with store.sessions() as session:
        accepted_count = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == str(child.id),
                    AuditEvent.action == "TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                )
            )
            or 0
        )
    await store.aclose()

    assert accepted.accepted_parent_omissions_attempt == parent.attempt_count
    assert replay.accepted_parent_omissions_attempt == parent.attempt_count
    assert sum(record.idempotent_replay for record in (accepted, replay)) == 1
    assert accepted_count == 1


@pytest.mark.asyncio
async def test_database_retry_omitted_files_reopens_only_selected_rows_and_keeps_history(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'retry-omitted.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("retry-omitted", "retry-omitted@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    await store.agrant_connection(user.id, source.id, user.id)
    await store.agrant_connection(user.id, destination.id, user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    transfer.status = "COMPLETED_WITH_WARNINGS"
    transfer.attempt_count = 4
    transfer.files_total = 4
    transfer.files_completed = 4
    transfer.bytes_total = 100
    transfer.bytes_transferred = 10
    transfer.warning_count = 3
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": f"/source/{name}",
                "destination_path": f"/destination/{name}",
                "size_bytes": size,
            }
            for name, size in (
                ("copied.bin", 10),
                ("omitted-one.rar", 20),
                ("omitted-two.rar", 21),
                ("legacy-skip.bin", 7),
            )
        ],
    )
    items = await store.alist_transfer_manifest_items(transfer.id, limit=10)
    by_name = {item.source_path.rsplit("/", 1)[-1]: item for item in items}
    now = datetime.now(UTC)
    async with store.sessions() as session:
        async with session.begin():
            transfer_row = await session.get(TransferJob, transfer.id)
            assert transfer_row is not None
            for name, status_value in (
                ("copied.bin", "COPIED"),
                ("omitted-one.rar", "SKIPPED"),
                ("omitted-two.rar", "SKIPPED"),
                ("legacy-skip.bin", "SKIPPED"),
            ):
                item_row = await session.get(TransferManifestItem, by_name[name].id)
                assert item_row is not None
                item_row.status = status_value
                if name.startswith("omitted-"):
                    item_row.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
                    item_row.omitted_at = now
                    item_row.omitted_by = user.id
            selected_row = await session.get(TransferManifestItem, by_name["omitted-one.rar"].id)
            assert selected_row is not None
            session.add(
                AuditEvent(
                    actor_user_id=user.id,
                    action="TRANSFER_FILE_OMITTED",
                    resource_type="transfer",
                    resource_id=str(transfer.id),
                    result="SUCCESS",
                    event_metadata={"manifest_item_id": str(selected_row.id)},
                )
            )

    request_id = uuid.uuid4()
    result = await store.aretry_omitted_transfer_items(
        transfer.id,
        item_ids=[by_name["omitted-one.rar"].id],
        actor_id=user.id,
        expected_attempt=4,
        request_id=request_id,
    )
    replay = await store.aretry_omitted_transfer_items(
        transfer.id,
        item_ids=[by_name["omitted-one.rar"].id],
        actor_id=user.id,
        expected_attempt=4,
        request_id=request_id,
    )
    with pytest.raises(TransferRecoveryConflictError):
        await store.aretry_omitted_transfer_items(
            transfer.id,
            item_ids=[by_name["omitted-two.rar"].id],
            actor_id=user.id,
            expected_attempt=4,
            request_id=request_id,
        )

    async with store.sessions() as session:
        selected_row = await session.get(TransferManifestItem, by_name["omitted-one.rar"].id)
        untouched_row = await session.get(TransferManifestItem, by_name["omitted-two.rar"].id)
        legacy_row = await session.get(TransferManifestItem, by_name["legacy-skip.bin"].id)
        stored_transfer = await session.get(TransferJob, transfer.id)
        audit_actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action).where(AuditEvent.resource_id == str(transfer.id))
                )
            ).all()
        )
        assert selected_row is not None
        assert untouched_row is not None and legacy_row is not None and stored_transfer is not None
        selected_state = (
            selected_row.status,
            selected_row.destination_path,
            selected_row.omission_kind,
            selected_row.omitted_at,
            selected_row.omitted_by,
        )
        untouched_state = (untouched_row.status, untouched_row.omission_kind)
        legacy_state = (legacy_row.status, legacy_row.omission_kind)
        transfer_state = (
            stored_transfer.status,
            stored_transfer.attempt_count,
            stored_transfer.files_completed,
            stored_transfer.warning_count,
            stored_transfer.bytes_transferred,
            stored_transfer.source_file_error_policy,
        )
    await store.aclose()

    assert result.attempt_count == replay.attempt_count == 5
    assert replay.idempotent_replay is True
    assert selected_state == ("PENDING", "/destination/omitted-one.rar", None, None, None)
    assert untouched_state == ("SKIPPED", "USER_ACCEPTED_SOURCE_FAILURE")
    assert legacy_state == ("SKIPPED", None)
    assert transfer_state == ("QUEUED", 5, 3, 2, 10, "STOP")
    assert audit_actions.count("TRANSFER_FILE_OMITTED") == 1
    assert audit_actions.count("TRANSFER_OMITTED_FILES_RETRIED") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("active", TransferRecoveryConflictError),
        ("reserved", TransferRecoveryConflictError),
        ("credentials", TransferRecoveryConflictError),
        ("copy_started", TransferRecoveryConflictError),
        ("legacy_skip", TransferRecoveryConflictError),
        ("foreign_item", TransferRecoveryNotFoundError),
        ("duplicate_ids", TransferRecoveryConflictError),
    ],
)
async def test_memory_retry_omitted_files_rejects_unsafe_state_and_selection(
    failure, expected_error
):
    store = MemoryStore()
    user = store.add_user(f"retry-{failure}", f"retry-{failure}@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    transfer.status = "COMPLETED_WITH_WARNINGS"
    transfer.attempt_count = 2
    await store.asave_transfer(transfer)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {"source_path": "/source/omitted.rar", "destination_path": "/destination/omitted.rar"},
            {"source_path": "/source/legacy.rar", "destination_path": "/destination/legacy.rar"},
        ],
    )
    items = await store.alist_transfer_manifest_items(transfer.id, limit=10)
    explicit = next(item for item in items if item.source_path.endswith("omitted.rar"))
    legacy = next(item for item in items if item.source_path.endswith("legacy.rar"))
    for item in items:
        item.status = "SKIPPED"
    explicit.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    explicit.omitted_at = datetime.now(UTC)
    selected_ids = [explicit.id]
    if failure == "active":
        transfer.status = "RUNNING"
    elif failure == "reserved":
        transfer.dispatch_token = uuid.uuid4()
    elif failure == "credentials":
        source.credential_version += 1
    elif failure == "copy_started":
        explicit.copy_started = True
    elif failure == "legacy_skip":
        selected_ids = [legacy.id]
    elif failure == "foreign_item":
        selected_ids = [uuid.uuid4()]
    elif failure == "duplicate_ids":
        selected_ids = [explicit.id, explicit.id]

    with pytest.raises(expected_error):
        await store.aretry_omitted_transfer_items(
            transfer.id,
            item_ids=selected_ids,
            actor_id=user.id,
            expected_attempt=2,
            request_id=uuid.uuid4(),
        )


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
    suffix = uuid.uuid4().hex
    user = await store.acreate_user(
        f"pg-recovery-{suffix}", f"pg-recovery-{suffix}@example.test", "hash"
    )
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
    assert attempt_count == 3
    assert audit_count == 1
    assert all(result.status == "QUEUED" and result.attempt_count == 3 for result in results)
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                delete(AuditEvent).where(AuditEvent.resource_id == str(transfer.id))
            )
            await session.execute(
                delete(TransferNotificationOutbox).where(
                    TransferNotificationOutbox.transfer_job_id == transfer.id
                )
            )
            await session.execute(
                delete(TransferEvent).where(TransferEvent.transfer_job_id == transfer.id)
            )
            await session.execute(
                delete(TransferEvaluation).where(
                    TransferEvaluation.transfer_job_id == transfer.id
                )
            )
            await session.execute(
                delete(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == transfer.id
                )
            )
            await session.execute(delete(TransferJob).where(TransferJob.id == transfer.id))
            await session.execute(
                delete(StorageConnection).where(
                    StorageConnection.id.in_([source.id, destination.id])
                )
            )
            await session.execute(delete(User).where(User.id == user.id))
    await store.aclose()


@pytest.mark.asyncio
async def test_postgres_parent_retry_racing_child_dispatch_requires_new_acceptance():
    database_url = os.environ.get("CTM_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("CTM_TEST_POSTGRES_URL is not configured")
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    suffix = uuid.uuid4().hex
    user = await store.acreate_user(
        f"pg-parent-{suffix}", f"pg-parent-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection(
        "Destination", "MOCK", user.id, uuid.uuid4(), b""
    )
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/parent"], "/destination"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/child"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )
    await store.aadd_transfer_manifest_items(
        parent.id,
        [
            {
                "source_path": "/parent/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 100,
            }
        ],
    )
    async with store.sessions() as session:
        async with session.begin():
            parent_row = await session.get(TransferJob, parent.id)
            omitted_row = await session.scalar(
                select(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == parent.id
                )
            )
            assert parent_row is not None and omitted_row is not None
            parent_row.status = "COMPLETED_WITH_WARNINGS"
            omitted_row.status = "SKIPPED"
            omitted_row.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
            omitted_row.omitted_at = datetime.now(UTC)

    request_id = uuid.uuid4()
    results = await asyncio.gather(
        *[
            store.aaccept_parent_omissions(
                child.id,
                actor_id=user.id,
                expected_parent_attempt=parent.attempt_count,
                request_id=request_id,
            )
            for _ in range(2)
        ]
    )
    assert sum(result.idempotent_replay for result in results) == 1
    reservations = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    assert [record.id for record in reservations.admitted] == [child.id]
    dispatch_token = reservations.admitted[0].dispatch_token
    assert dispatch_token is not None

    parent_retry_locked = asyncio.Event()
    allow_parent_retry_commit = asyncio.Event()

    async def update_parent_attempt() -> None:
        async with store.sessions() as session:
            async with session.begin():
                parent_row = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == parent.id)
                    .with_for_update()
                )
                assert parent_row is not None
                parent_retry_locked.set()
                await allow_parent_retry_commit.wait()
                parent_row.attempt_count += 1
                parent_row.status = "COMPLETED_WITH_WARNINGS"

    async def start_child() -> bool:
        return await store.astart_transfer_dispatch(
            child.id, dispatch_token, datetime.now(UTC)
        )

    retry_task = asyncio.create_task(update_parent_attempt())
    start_task: asyncio.Task[bool] | None = None
    try:
        await parent_retry_locked.wait()
        start_task = asyncio.create_task(start_child())
        await asyncio.sleep(0.05)
        assert not start_task.done()
        allow_parent_retry_commit.set()
        await retry_task
        started = await start_task
        updated_child = await store.aget_transfer(child.id)
    finally:
        allow_parent_retry_commit.set()
        if not retry_task.done():
            await retry_task
        if start_task is not None and not start_task.done():
            await start_task
        async with store.sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(AuditEvent).where(
                        AuditEvent.resource_id == str(child.id)
                    )
                )
                await session.execute(
                    delete(TransferNotificationOutbox).where(
                        TransferNotificationOutbox.transfer_job_id.in_([parent.id, child.id])
                    )
                )
                await session.execute(
                    delete(TransferEvent).where(
                        TransferEvent.transfer_job_id.in_([parent.id, child.id])
                    )
                )
                await session.execute(
                    delete(TransferEvaluation).where(
                        TransferEvaluation.transfer_job_id.in_([parent.id, child.id])
                    )
                )
                await session.execute(
                    delete(TransferManifestItem).where(
                        TransferManifestItem.transfer_job_id.in_([parent.id, child.id])
                    )
                )
                await session.execute(delete(TransferJob).where(TransferJob.id == child.id))
                await session.execute(delete(TransferJob).where(TransferJob.id == parent.id))
                await session.execute(
                    delete(StorageConnection).where(
                        StorageConnection.id.in_([source.id, destination.id])
                    )
                )
                await session.execute(delete(User).where(User.id == user.id))
        await store.aclose()

    assert started is False
    assert updated_child is not None
    assert updated_child.status == "QUEUED"
    assert updated_child.wait_reason == "BLOCKED_PARENT_OMISSIONS"


@pytest.mark.asyncio
async def test_postgres_retries_selected_omissions_once_for_concurrent_same_request():
    database_url = os.environ.get("CTM_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("CTM_TEST_POSTGRES_URL is not configured")
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    suffix = uuid.uuid4().hex
    user = await store.acreate_user(
        f"pg-omitted-retry-{suffix}", f"pg-omitted-retry-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection(
        "Destination", "MOCK", user.id, uuid.uuid4(), b""
    )
    await store.agrant_connection(user.id, source.id, user.id)
    await store.agrant_connection(user.id, destination.id, user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 100,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    async with store.sessions() as session:
        async with session.begin():
            item_row = await session.get(TransferManifestItem, item.id)
            assert item_row is not None
            item_row.status = "SKIPPED"
            item_row.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
            item_row.omitted_at = datetime.now(UTC)
    terminal = await store.aget_transfer(transfer.id)
    assert terminal is not None
    terminal.status = "COMPLETED_WITH_WARNINGS"
    terminal.attempt_count = 2
    terminal.files_total = 1
    terminal.files_completed = 1
    terminal.bytes_total = 100
    terminal.warning_count = 1
    terminal.completed_at = datetime.now(UTC)
    await store.asave_transfer(terminal)

    request_id = uuid.uuid4()
    results = await asyncio.gather(
        *[
            store.aretry_omitted_transfer_items(
                transfer.id,
                item_ids=[item.id],
                actor_id=user.id,
                expected_attempt=2,
                request_id=request_id,
            )
            for _ in range(2)
        ]
    )
    async with store.sessions() as session:
        async with session.begin():
            persisted_attempt = await session.scalar(
                select(TransferJob.attempt_count).where(TransferJob.id == transfer.id)
            )
            audit_count = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_id == str(transfer.id),
                    AuditEvent.action == "TRANSFER_OMITTED_FILES_RETRIED",
                )
            )
            await session.execute(
                delete(AuditEvent).where(AuditEvent.resource_id == str(transfer.id))
            )
            await session.execute(
                delete(TransferNotificationOutbox).where(
                    TransferNotificationOutbox.transfer_job_id == transfer.id
                )
            )
            await session.execute(
                delete(TransferEvent).where(TransferEvent.transfer_job_id == transfer.id)
            )
            await session.execute(
                delete(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == transfer.id
                )
            )
            await session.execute(delete(TransferJob).where(TransferJob.id == transfer.id))
            await session.execute(
                delete(StorageConnection).where(
                    StorageConnection.id.in_([source.id, destination.id])
                )
            )
            await session.execute(delete(User).where(User.id == user.id))
    await store.aclose()

    assert sum(result.idempotent_replay for result in results) == 1
    assert all(result.attempt_count == 3 for result in results)
    assert persisted_attempt == 3
    assert audit_count == 1
