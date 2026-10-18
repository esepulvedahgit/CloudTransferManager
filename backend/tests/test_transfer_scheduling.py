import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.main import TransferCreate, create_app
from app.security.passwords import PasswordService
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore, TransferRecord
from app.services.source_inventory import InventoryProgress
from app.services.transfer_evaluation import EvaluationClaim, EvaluationRecord
from app.services.transfer_scheduling import (
    TransferReservation,
    admission_wait_reason,
    dependency_wait_reason,
    evaluation_wait_reason,
    transfer_requires_staging,
)
from app.settings import Settings


def _payload(**overrides):
    values = {
        "source_connection_id": "aab67f6b-d842-4865-8423-b67cdaf7353a",
        "destination_connection_id": "b82fb96d-335f-44e9-a32f-5a762bc52799",
        "source_paths": ["/Backups"],
        "destination_path": "/Migraciones",
    }
    values.update(overrides)
    return values


def test_transfer_create_preserves_utc_schedule_mode_and_optional_parent():
    scheduled_at = datetime.now(UTC) + timedelta(hours=1)
    parent_id = "6ca62b0a-8225-4a09-babb-9cafc45a9ef8"

    payload = TransferCreate(
        **_payload(
            scheduled_at=scheduled_at,
            schedule_timezone="America/Santiago",
            execution_mode="EXCLUSIVE",
            depends_on_transfer_id=parent_id,
        )
    )

    assert payload.model_dump()["scheduled_at"] == scheduled_at
    assert payload.model_dump()["schedule_timezone"] == "America/Santiago"
    assert payload.model_dump()["execution_mode"] == "EXCLUSIVE"
    assert str(payload.model_dump()["depends_on_transfer_id"]) == parent_id


def test_transfer_create_rejects_naive_scheduled_datetime():
    with pytest.raises(ValidationError, match="zona horaria"):
        naive_future = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
        TransferCreate(**_payload(scheduled_at=naive_future.isoformat()))


def test_auto_admission_allows_parallel_work_until_the_global_limit():
    active = [
        TransferReservation(
            transfer_id="active",
            source_connection_id="source-a",
            destination_connection_id="destination-a",
            execution_mode="AUTO",
            uses_staging=False,
        )
    ]

    assert admission_wait_reason(
        source_connection_id="source-b",
        destination_connection_id="destination-b",
        execution_mode="AUTO",
        uses_staging=False,
        active=active,
        max_concurrent=2,
    ) is None
    assert admission_wait_reason(
        source_connection_id="source-c",
        destination_connection_id="destination-c",
        execution_mode="AUTO",
        uses_staging=False,
        active=active * 2,
        max_concurrent=2,
    ) == "WAITING_CAPACITY"


def test_nextcloud_admission_has_its_own_capacity_limit():
    active = [
        TransferReservation(
            transfer_id="active-nextcloud",
            source_connection_id="source-a",
            destination_connection_id="nextcloud-a",
            execution_mode="AUTO",
            uses_staging=False,
            uses_nextcloud=True,
        )
    ]

    assert admission_wait_reason(
        source_connection_id="source-b",
        destination_connection_id="nextcloud-b",
        execution_mode="AUTO",
        uses_staging=False,
        uses_nextcloud=True,
        active=active,
        max_concurrent=3,
        max_nextcloud_concurrent=1,
    ) == "WAITING_CAPACITY"


def test_exclusive_admission_waits_for_active_transfers_to_finish():
    active = [
        TransferReservation(
            transfer_id="active",
            source_connection_id="source",
            destination_connection_id="destination",
            execution_mode="AUTO",
            uses_staging=False,
        )
    ]

    assert admission_wait_reason(
        source_connection_id="other-source",
        destination_connection_id="other-destination",
        execution_mode="EXCLUSIVE",
        uses_staging=False,
        active=active,
        max_concurrent=2,
    ) == "WAITING_EXCLUSIVE"


def test_transfer_dependencies_only_open_after_success_including_warnings():
    assert dependency_wait_reason(None) is None
    assert dependency_wait_reason("COMPLETED") is None
    assert dependency_wait_reason("COMPLETED_WITH_WARNINGS") is None
    assert dependency_wait_reason("RUNNING") == "WAITING_PARENT"
    assert dependency_wait_reason("FAILED") == "BLOCKED_PARENT_FAILED"
    assert dependency_wait_reason("CANCELLED") == "BLOCKED_PARENT_FAILED"


def test_transfer_dependencies_require_current_attempt_acceptance_for_omissions():
    assert (
        dependency_wait_reason(
            "COMPLETED_WITH_WARNINGS",
            parent_omitted_count=1,
            parent_attempt_count=3,
        )
        == "BLOCKED_PARENT_OMISSIONS"
    )
    assert (
        dependency_wait_reason(
            "COMPLETED_WITH_WARNINGS",
            parent_omitted_count=1,
            parent_attempt_count=3,
            accepted_parent_omissions_attempt=3,
        )
        is None
    )
    assert (
        dependency_wait_reason(
            "COMPLETED_WITH_WARNINGS",
            parent_omitted_count=1,
            parent_attempt_count=4,
            accepted_parent_omissions_attempt=3,
        )
        == "BLOCKED_PARENT_OMISSIONS"
    )
    for status in ("FAILED", "CANCELLED"):
        assert (
            dependency_wait_reason(
                status,
                parent_omitted_count=1,
                parent_attempt_count=3,
                accepted_parent_omissions_attempt=3,
            )
            == "BLOCKED_PARENT_FAILED"
        )


def test_evaluation_wait_reason_distinguishes_pending_blocked_and_approved():
    transfer_id = uuid.uuid4()
    evaluation = EvaluationRecord(id=uuid.uuid4(), transfer_id=transfer_id, status="PENDING")

    assert evaluation_wait_reason(None) is None
    assert evaluation_wait_reason(evaluation) == "WAITING_EVALUATION"
    evaluation.status = "EVALUATING"
    assert evaluation_wait_reason(evaluation) == "WAITING_EVALUATION"
    evaluation.status = "BLOCKED"
    assert evaluation_wait_reason(evaluation) == "BLOCKED_EVALUATION"
    evaluation.status = "ERROR"
    assert evaluation_wait_reason(evaluation) == "BLOCKED_EVALUATION"
    evaluation.status = "WAITING_PARENT"
    assert evaluation_wait_reason(evaluation) == "WAITING_PARENT"
    evaluation.status = "APPROVED"
    assert evaluation_wait_reason(evaluation) is None


@pytest.mark.asyncio
async def test_database_dispatcher_keeps_pending_and_blocked_evaluations_in_programaciones(
    tmp_path,
):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'evaluation-gate.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("evaluation-owner", "evaluation@example.test", "hash")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    scheduled_at = datetime.now(UTC) + timedelta(minutes=1)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=scheduled_at,
    )
    waiting = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    pending = await store.aget_transfer(transfer.id)
    claim = (await store.aclaim_due_evaluations(scheduled_at + timedelta(seconds=1)))[0]
    assert await store.afinish_evaluation(
        claim,
        status="BLOCKED",
        progress=InventoryProgress(files_found=5001, count_complete=False),
        error_code="TRANSFER_FILE_LIMIT_EXCEEDED",
        message="La selección supera el límite.",
        now=scheduled_at + timedelta(seconds=2),
    )
    blocked = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=2), max_concurrent=1
    )
    current = await store.aget_transfer(transfer.id)
    programaciones = await store.alist_transfers(pending_only=True)
    await store.aclose()

    assert waiting.admitted == []
    assert pending is not None and pending.wait_reason == "WAITING_EVALUATION"
    assert blocked.admitted == []
    assert current is not None and current.wait_reason == "BLOCKED_EVALUATION"
    assert transfer.id in {item.id for item in programaciones}


@pytest.mark.asyncio
async def test_approved_evaluation_allows_dispatch_when_schedule_is_due(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'approved-evaluation.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("approved-owner", "approved@example.test", "hash")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    scheduled_at = datetime.now(UTC) + timedelta(minutes=1)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=scheduled_at,
    )
    claim = (await store.aclaim_due_evaluations(datetime.now(UTC)))[0]
    assert await store.afinish_evaluation(
        claim,
        status="APPROVED",
        progress=InventoryProgress(files_found=4180, count_complete=True),
        error_code=None,
        message=None,
        now=datetime.now(UTC),
    )

    batch = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    await store.aclose()

    assert [job.id for job in batch.admitted] == [transfer.id]


@pytest.mark.asyncio
async def test_exclusive_transfer_waiting_for_evaluation_does_not_block_later_work(tmp_path):
    from sqlalchemy import update

    from app.models import TransferJob

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'exclusive-evaluation.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("exclusive-owner", "exclusive@example.test", "hash")
    connections = [
        await store.acreate_connection(
            f"Drive {index}", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
        )
        for index in range(4)
    ]
    scheduled_at = datetime.now(UTC) + timedelta(minutes=1)
    exclusive = await store.acreate_transfer(
        user.id,
        connections[0].id,
        connections[1].id,
        ["/exclusive"],
        "/destination",
        execution_mode="EXCLUSIVE",
        scheduled_at=scheduled_at,
    )
    async with store.sessions() as session:
        await session.execute(
            update(TransferJob)
            .where(TransferJob.id == exclusive.id)
            .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    compatible = await store.acreate_transfer(
        user.id,
        connections[2].id,
        connections[3].id,
        ["/compatible"],
        "/destination",
        conflict_policy="SKIP",
    )

    batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=2)
    waiting = await store.aget_transfer(exclusive.id)
    await store.aclose()

    assert exclusive.id not in {job.id for job in batch.admitted}
    assert [job.id for job in batch.admitted] == [compatible.id]
    assert waiting is not None and waiting.wait_reason == "WAITING_EVALUATION"


@pytest.mark.asyncio
async def test_changed_file_limit_invalidates_approval_and_starts_new_generation(tmp_path):
    from sqlalchemy import update

    from app.models import TransferEvaluation

    store = DatabaseStore(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'changed-limit.sqlite'}",
            transfer_max_files=2,
        )
    )
    await store.create_schema()
    user = await store.acreate_user("limit-owner", "limit@example.test", "hash")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    scheduled_at = datetime.now(UTC) + timedelta(minutes=1)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=scheduled_at,
    )
    async with store.sessions() as session:
        await session.execute(
            update(TransferEvaluation)
            .where(TransferEvaluation.transfer_job_id == transfer.id)
            .values(
                status="APPROVED",
                generation=3,
                sequence=8,
                files_found=3,
                count_complete=True,
                max_files=5000,
            )
        )
        await session.commit()

    batch = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert batch.admitted == []
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "PENDING"
    assert current.evaluation.generation == 4
    assert current.evaluation.sequence == 0
    assert current.evaluation.files_found is None
    assert current.evaluation.max_files == 2
    assert current.wait_reason == "WAITING_EVALUATION"


@pytest.mark.asyncio
async def test_credential_change_blocks_before_reserving_copy_dispatch(tmp_path):
    from sqlalchemy import update

    from app.models import StorageConnection, TransferEvaluation

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'changed-credentials.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("credential-owner", "credential@example.test", "hash")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    scheduled_at = datetime.now(UTC) + timedelta(minutes=1)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=scheduled_at,
    )
    async with store.sessions() as session:
        await session.execute(
            update(TransferEvaluation)
            .where(TransferEvaluation.transfer_job_id == transfer.id)
            .values(status="APPROVED", files_found=4, count_complete=True)
        )
        await session.execute(
            update(StorageConnection)
            .where(StorageConnection.id == source.id)
            .values(credential_version=StorageConnection.credential_version + 1)
        )
        await session.commit()

    batch = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert batch.admitted == []
    assert current is not None and current.dispatch_token is None
    assert current.wait_reason == "BLOCKED_CONNECTION_CHANGED"


@pytest.mark.asyncio
async def test_memory_dispatcher_gates_pending_evaluation_without_exclusive_barrier():
    store = MemoryStore()
    owner = store.add_user("memory-schedule", "memory-schedule@example.test", "hash")
    source = store.create_connection("Source", "MOCK", owner.id)
    destination = store.create_connection("Destination", "MOCK", owner.id)
    now = datetime.now(UTC)
    exclusive = store.create_transfer(
        owner.id,
        source.id,
        destination.id,
        ["/exclusive"],
        "/destination",
        scheduled_at=now + timedelta(minutes=1),
        execution_mode="EXCLUSIVE",
    )
    compatible = store.create_transfer(
        owner.id,
        source.id,
        destination.id,
        ["/compatible"],
        "/destination",
        conflict_policy="SKIP",
    )

    batch = await store.adispatch_due_transfers(now + timedelta(minutes=2), max_concurrent=2)

    assert [transfer.id for transfer in batch.admitted] == [compatible.id]
    assert exclusive.wait_reason == "WAITING_EVALUATION"


def test_staging_transfer_waits_for_the_single_staging_slot():
    active = [
        TransferReservation(
            transfer_id="staged",
            source_connection_id="smb",
            destination_connection_id="drive-a",
            execution_mode="AUTO",
            uses_staging=True,
        )
    ]

    assert transfer_requires_staging("SMB", "GOOGLE_DRIVE")
    assert transfer_requires_staging("GOOGLE_DRIVE", "MEGA")
    assert transfer_requires_staging("GOOGLE_DRIVE", "SEAFILE", "RCLONE", "SEAFILE_API")
    assert transfer_requires_staging("GOOGLE_DRIVE", "SEAFILE")
    assert transfer_requires_staging("GOOGLE_DRIVE", "NEXTCLOUD")
    assert not transfer_requires_staging("GOOGLE_DRIVE", "NEXTCLOUD", conflict_policy="SKIP")
    assert admission_wait_reason(
        source_connection_id="drive-b",
        destination_connection_id="drive-c",
        execution_mode="AUTO",
        uses_staging=True,
        active=active,
        max_concurrent=2,
    ) == "WAITING_STAGING"


@pytest.mark.asyncio
async def test_database_dispatcher_admits_parallel_jobs_and_holds_an_exclusive_barrier(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'scheduled.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    connections = []
    for index in range(6):
        connection_id = uuid.uuid4()
        connections.append(
            await store.acreate_connection(
                f"Drive {index}", "GOOGLE_DRIVE", user.id, connection_id, b"encrypted"
            )
        )

    transfers = []
    for index, mode in enumerate(("AUTO", "AUTO", "EXCLUSIVE")):
        transfers.append(
            await store.acreate_transfer(
                user.id,
                connections[index * 2].id,
                connections[index * 2 + 1].id,
                ["/source"],
                "/destination",
                conflict_policy="SKIP",
                execution_mode=mode,
            )
        )
    scheduled_at = datetime.now(UTC) + timedelta(minutes=5)
    future = await store.acreate_transfer(
        user.id,
        connections[0].id,
        connections[1].id,
        ["/later"],
        "/destination",
        conflict_policy="SKIP",
        scheduled_at=scheduled_at,
        schedule_timezone="America/Santiago",
    )

    batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=2)
    jobs = {job.id: job for job in await store.alist_transfers()}
    await store.aclose()

    assert {job.id for job in batch.admitted} == {transfers[0].id, transfers[1].id}
    assert all(job.dispatch_token is not None for job in batch.admitted)
    assert jobs[transfers[2].id].wait_reason == "WAITING_EXCLUSIVE"
    assert jobs[future.id].status == "SCHEDULED"
    assert jobs[future.id].wait_reason == "WAITING_UNTIL"


@pytest.mark.asyncio
async def test_database_dispatcher_enforces_nextcloud_connection_capacity(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'nextcloud-capacity.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destinations = [
        await store.acreate_connection(
            f"Nextcloud {index}", "NEXTCLOUD", user.id, uuid.uuid4(), b"x"
        )
        for index in range(2)
    ]
    transfers = [
        await store.acreate_transfer(
            user.id, source.id, destination.id, ["/source"], "/destination"
        )
        for destination in destinations
    ]

    batch = await store.adispatch_due_transfers(
        datetime.now(UTC), max_concurrent=3, max_nextcloud_concurrent=1
    )

    assert len(batch.admitted) == 1
    waiting = next(item for item in batch.changed if item.id == transfers[1].id)
    assert waiting.wait_reason == "WAITING_CAPACITY"
    await store.aclose()


@pytest.mark.asyncio
async def test_dispatcher_keeps_due_scheduled_transfer_until_evaluation_completes(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'catch-up.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    scheduled_at = datetime.now(UTC) + timedelta(seconds=30)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=scheduled_at,
        schedule_timezone="UTC",
    )

    batch = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert batch.admitted == []
    assert current is not None and current.dispatch_token is None
    assert current.wait_reason == "WAITING_EVALUATION"


@pytest.mark.asyncio
async def test_dispatcher_releases_child_after_parent_completes_with_warnings(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'parent-success.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/first"], "/destination"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/second"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )

    waiting_batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    waiting_child = await store.aget_transfer(child.id)
    parent.status = "COMPLETED_WITH_WARNINGS"
    await store.asave_transfer(parent)
    released_batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    await store.aclose()

    assert child.id not in {job.id for job in waiting_batch.admitted}
    assert waiting_child is not None
    assert waiting_child.wait_reason == "WAITING_PARENT"
    assert [job.id for job in released_batch.admitted] == [child.id]


@pytest.mark.asyncio
async def test_dispatcher_blocks_parent_omissions_until_current_attempt_is_accepted(tmp_path):
    from sqlalchemy import select

    from app.models import TransferJob, TransferManifestItem

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'parent-omissions.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("parent-owner", "parent@example.test", "hash")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/first"], "/destination"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/second"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )
    await store.aadd_transfer_manifest_items(
        parent.id,
        [
            {
                "source_path": "/first/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 8,
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

    blocked = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    blocked_child = await store.aget_transfer(child.id)
    assert child.id not in {job.id for job in blocked.admitted}
    assert blocked_child is not None
    assert blocked_child.wait_reason == "BLOCKED_PARENT_OMISSIONS"

    async with store.sessions() as session:
        async with session.begin():
            child_row = await session.get(TransferJob, child.id)
            assert child_row is not None
            child_row.accepted_parent_omissions_attempt = parent.attempt_count
    released = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    await store.aclose()

    assert [job.id for job in released.admitted] == [child.id]


@pytest.mark.asyncio
async def test_dispatcher_keeps_child_blocked_when_parent_failed(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'parent-failure.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/first"], "/destination"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/second"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )
    parent.status = "FAILED"
    await store.asave_transfer(parent)

    batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    blocked_child = await store.aget_transfer(child.id)
    await store.aclose()

    assert child.id not in {job.id for job in batch.admitted}
    assert blocked_child is not None
    assert blocked_child.wait_reason == "BLOCKED_PARENT_FAILED"


@pytest.mark.asyncio
async def test_replaced_dispatch_token_cannot_start_a_stale_celery_delivery(tmp_path):
    from datetime import timedelta

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'dispatch-token.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    first = (await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)).admitted[0]
    assert first.dispatch_token is not None
    await store.arelease_transfer_dispatch(transfer.id, first.dispatch_token)
    second = (await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)).admitted[0]

    old_started = await store.astart_transfer_dispatch(
        transfer.id, first.dispatch_token, datetime.now(UTC) + timedelta(seconds=1)
    )
    current_started = await store.astart_transfer_dispatch(
        transfer.id, second.dispatch_token, datetime.now(UTC) + timedelta(seconds=2)
    )
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert old_started is False
    assert current_started is True
    assert current is not None
    assert current.dispatch_token == second.dispatch_token
    assert current.execution_started_at is not None


@pytest.mark.asyncio
async def test_pending_transfer_can_be_reprogrammed_but_an_admitted_job_cannot(tmp_path):
    from datetime import timedelta

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'reprogram.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    later = datetime.now(UTC) + timedelta(hours=2)

    updated = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=later,
        schedule_timezone="America/Santiago",
        execution_mode="EXCLUSIVE",
        depends_on_transfer_id=None,
    )
    assert updated is not None
    assert updated.status == "SCHEDULED"
    assert updated.execution_mode == "EXCLUSIVE"
    assert updated.scheduled_at is not None

    claim = (await store.aclaim_due_evaluations(datetime.now(UTC)))[0]
    assert await store.afinish_evaluation(
        claim,
        status="APPROVED",
        progress=InventoryProgress(files_found=1, count_complete=True),
        error_code=None,
        message=None,
        now=datetime.now(UTC),
    )

    claimed = (await store.adispatch_due_transfers(later, max_concurrent=1)).admitted[0]
    rejected_edit = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=later + timedelta(hours=1),
        schedule_timezone="UTC",
        execution_mode="AUTO",
        depends_on_transfer_id=None,
    )
    await store.aclose()

    assert claimed.dispatch_token is not None
    assert rejected_edit is None


@pytest.mark.asyncio
async def test_schedule_conversion_creates_evaluation_and_date_change_preserves_approval(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'schedule-conversion.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("conversion-owner", "conversion@example.test", "hash")
    source = await store.acreate_connection("Source", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Destination", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    scheduled_at = datetime.now(UTC) + timedelta(hours=1)

    converted = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=scheduled_at,
        schedule_timezone="UTC",
        execution_mode="AUTO",
        depends_on_transfer_id=None,
    )
    assert converted is not None and converted.evaluation is not None
    assert converted.evaluation.status == "PENDING"

    claim = (await store.aclaim_due_evaluations(datetime.now(UTC)))[0]
    assert await store.afinish_evaluation(
        claim,
        status="APPROVED",
        progress=InventoryProgress(files_found=4, count_complete=True),
        error_code=None,
        message=None,
        now=datetime.now(UTC),
    )
    approved = await store.aget_transfer(transfer.id)
    assert approved is not None and approved.evaluation is not None
    next_date = scheduled_at + timedelta(hours=1)
    rescheduled = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=next_date,
        schedule_timezone="America/Santiago",
        execution_mode="AUTO",
        depends_on_transfer_id=None,
    )
    without_date = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=None,
        schedule_timezone="UTC",
        execution_mode="AUTO",
        depends_on_transfer_id=None,
    )
    await store.aclose()

    assert rescheduled is not None and rescheduled.evaluation is not None
    assert rescheduled.evaluation.status == "APPROVED"
    assert rescheduled.evaluation.generation == approved.evaluation.generation
    assert without_date is not None and without_date.evaluation is not None
    assert without_date.evaluation.status == "APPROVED"


@pytest.mark.asyncio
async def test_dependency_change_invalidates_approval_until_parent_succeeds(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'changed-parent.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("changed-parent-owner", "changed-parent@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/parent"], "/destination"
    )
    parent.status = "RUNNING"
    await store.asave_transfer(parent)
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/child"],
        "/destination",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    old_claim_token = uuid.uuid4()
    async with store.sessions() as session:
        from sqlalchemy import update

        from app.models import TransferEvaluation

        await session.execute(
            update(TransferEvaluation)
            .where(TransferEvaluation.transfer_job_id == child.id)
            .values(
                status="EVALUATING",
                files_found=4,
                count_complete=False,
                generation=3,
                claim_token=old_claim_token,
                attempt_count=1,
            )
        )
        await session.commit()

    updated = await store.aupdate_transfer_schedule(
        child.id,
        scheduled_at=child.scheduled_at,
        schedule_timezone="UTC",
        execution_mode="AUTO",
        depends_on_transfer_id=parent.id,
    )
    old_claim_write = await store.afinish_evaluation(
        EvaluationClaim(child.id, 3, old_claim_token),
        status="APPROVED",
        progress=InventoryProgress(files_found=4, count_complete=True),
        error_code=None,
        message=None,
        now=datetime.now(UTC),
    )
    waiting_claims = await store.aclaim_due_evaluations(datetime.now(UTC))
    parent.status = "COMPLETED"
    await store.asave_transfer(parent)
    ready_claims = await store.aclaim_due_evaluations(datetime.now(UTC) + timedelta(seconds=1))
    await store.aclose()

    assert updated is not None and updated.evaluation is not None
    assert updated.evaluation.generation == 4
    assert updated.evaluation.status == "WAITING_PARENT"
    assert updated.evaluation.files_found is None
    assert old_claim_write is False
    assert waiting_claims == []
    assert len(ready_claims) == 1 and ready_claims[0].transfer_id == child.id


@pytest.mark.asyncio
async def test_removing_schedule_does_not_bypass_a_pending_evaluation(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'removed-schedule.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("unscheduled-owner", "unscheduled@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )

    updated = await store.aupdate_transfer_schedule(
        transfer.id,
        scheduled_at=None,
        schedule_timezone="UTC",
        execution_mode="AUTO",
        depends_on_transfer_id=None,
    )
    batch = await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert updated is not None and updated.evaluation is not None
    assert batch.admitted == []
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "PENDING"
    assert current.wait_reason == "WAITING_EVALUATION"


@pytest.mark.asyncio
async def test_pending_transfer_cancellation_is_immediate_without_a_worker_reservation(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel-pending.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )

    cancelled, needs_worker_signal = await store.acancel_transfer(transfer.id, datetime.now(UTC))
    await store.aclose()

    assert cancelled is not None
    assert cancelled.status == "CANCELLED"
    assert cancelled.completed_at is not None
    assert needs_worker_signal is False


@pytest.mark.asyncio
async def test_cancelling_a_reserved_but_not_started_transfer_releases_its_slot(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel-reserved.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    reservation = (
        await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    ).admitted[0]
    cancelled, needs_worker_signal = await store.acancel_transfer(transfer.id, datetime.now(UTC))

    batch_after_cancel = await store.adispatch_due_transfers(
        datetime.now(UTC), max_concurrent=1
    )
    await store.aclose()

    assert reservation.dispatch_token is not None
    assert cancelled is not None
    assert cancelled.status == "CANCELLED"
    assert cancelled.dispatch_token is None
    assert needs_worker_signal is False
    assert transfer.id not in {job.id for job in batch_after_cancel.admitted}


@pytest.mark.asyncio
async def test_resume_clears_old_worker_reservation_before_dispatching_new_attempt(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'resume-reservation.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    old_attempt = (
        await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    ).admitted[0]
    assert old_attempt.dispatch_token is not None
    assert await store.astart_transfer_dispatch(
        transfer.id, old_attempt.dispatch_token, datetime.now(UTC)
    )
    failed = await store.aget_transfer(transfer.id)
    assert failed is not None
    failed.status = "FAILED"
    failed.completed_at = datetime.now(UTC)
    await store.asave_transfer(failed)

    resumed = await store.aresume_transfer(transfer.id)
    new_attempt = (
        await store.adispatch_due_transfers(datetime.now(UTC), max_concurrent=1)
    ).admitted[0]
    await store.afinish_transfer_dispatch(transfer.id, old_attempt.dispatch_token)
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert resumed is not None
    assert resumed.status == "QUEUED"
    assert resumed.dispatch_token is None
    assert resumed.execution_started_at is None
    assert new_attempt.dispatch_token is not None
    assert current is not None
    assert current.dispatch_token == new_attempt.dispatch_token


@pytest.mark.asyncio
async def test_stale_worker_heartbeat_fails_safely_and_releases_its_reservation(tmp_path):
    from datetime import timedelta

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'stale-worker.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    now = datetime.now(UTC)
    claimed = (await store.adispatch_due_transfers(now, max_concurrent=1)).admitted[0]
    assert claimed.dispatch_token is not None
    await store.astart_transfer_dispatch(
        transfer.id, claimed.dispatch_token, now - timedelta(minutes=2)
    )

    await store.adispatch_due_transfers(now, max_concurrent=1)
    current = await store.aget_transfer(transfer.id)
    events = await store.alist_transfer_events(transfer.id)
    await store.aclose()

    assert current is not None
    assert current.status == "FAILED"
    assert current.dispatch_token is None
    assert any(event["event_type"] == "WORKER_INTERRUPTED" for event in events)


@pytest.mark.asyncio
async def test_worker_start_revalidates_connection_versions_after_dispatch_reservation(tmp_path):
    from sqlalchemy import update

    from app.models import StorageConnection

    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'connection-version.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("admin", "admin@example.test", "hash", role="ADMIN")
    source = await store.acreate_connection("Drive A", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x")
    destination = await store.acreate_connection(
        "Drive B", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"x"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source"], "/destination"
    )
    now = datetime.now(UTC)
    claimed = (await store.adispatch_due_transfers(now, max_concurrent=1)).admitted[0]
    assert claimed.dispatch_token is not None
    async with store.sessions() as session:
        await session.execute(
            update(StorageConnection)
            .where(StorageConnection.id == source.id)
            .values(credential_version=StorageConnection.credential_version + 1)
        )
        await session.commit()

    started = await store.astart_transfer_dispatch(transfer.id, claimed.dispatch_token, now)
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert started is False
    assert current is not None
    assert current.status == "QUEUED"
    assert current.wait_reason == "BLOCKED_CONNECTION_CHANGED"
    assert current.dispatch_token is None


@pytest.mark.asyncio
async def test_background_only_returns_nonterminal_scheduled_transfers():
    store = MemoryStore()
    password = "CorrectHorseBatteryStaple123!"
    owner = store.add_user(
        "background-owner",
        "background-owner@example.test",
        PasswordService().hash(password),
    )
    other_owner = store.add_user(
        "background-other",
        "background-other@example.test",
        PasswordService().hash(password),
    )
    connection_ids = (uuid.uuid4(), uuid.uuid4())
    base_time = datetime.now(UTC)
    future = base_time + timedelta(days=2)
    background_specs = [
        ("SCHEDULED", {"scheduled_at": future}),
        ("QUEUED", {"wait_reason": "WAITING_CAPACITY"}),
        ("VALIDATING", {"execution_mode": "EXCLUSIVE"}),
        ("RUNNING", {"depends_on_transfer_id": uuid.uuid4()}),
        ("RETRYING", {"scheduled_at": future}),
        ("CANCELLING", {"wait_reason": "WAITING_CONNECTION"}),
    ]

    def add_transfer(requested_by, sequence, status, **metadata):
        transfer = TransferRecord(
            id=uuid.uuid4(),
            public_id=f"TRF-2026-{sequence:06d}",
            requested_by=requested_by,
            source_connection_id=connection_ids[0],
            destination_connection_id=connection_ids[1],
            source_paths=["/NAS"],
            destination_path="/Archive",
            status=status,
            created_at=base_time + timedelta(seconds=sequence),
            **metadata,
        )
        store.transfers[transfer.id] = transfer
        return transfer

    background_records = [
        add_transfer(owner.id, index + 1, status, **metadata)
        for index, (status, metadata) in enumerate(background_specs)
    ]
    terminal_specs = [
        ("COMPLETED", {"scheduled_at": future}),
        ("COMPLETED_WITH_WARNINGS", {"wait_reason": "WAITING_PARENT"}),
        ("FAILED", {"execution_mode": "EXCLUSIVE"}),
        ("CANCELLED", {"depends_on_transfer_id": uuid.uuid4()}),
    ]
    terminal_records = [
        add_transfer(owner.id, index + 20, status, **metadata)
        for index, (status, metadata) in enumerate(terminal_specs)
    ]
    ordinary_queued = add_transfer(owner.id, 30, "QUEUED")
    foreign_scheduled = add_transfer(
        other_owner.id, 40, "SCHEDULED", scheduled_at=future
    )

    manifest_entries = [
        {"source_path": "/folder", "destination_path": "/Archive/folder", "is_dir": True},
        {
            "source_path": "/copied.bin",
            "destination_path": "/Archive/copied.bin",
            "is_dir": False,
            "size_bytes": 4,
        },
        {
            "source_path": "/failed.bin",
            "destination_path": "/Archive/failed.bin",
            "is_dir": False,
            "size_bytes": 3,
        },
        {
            "source_path": "/skipped.bin",
            "destination_path": "/Archive/skipped.bin",
            "is_dir": False,
            "size_bytes": 2,
        },
        {
            "source_path": "/pending.bin",
            "destination_path": "/Archive/pending.bin",
            "is_dir": False,
            "size_bytes": 1,
        },
    ]
    await store.aadd_transfer_manifest_items(background_records[0].id, manifest_entries)
    manifest_items = await store.alist_transfer_manifest_items(
        background_records[0].id,
        statuses={"PENDING"},
        after_id=None,
        limit=10,
    )
    manifest_statuses = {
        "/copied.bin": "COPIED",
        "/failed.bin": "FAILED",
        "/skipped.bin": "SKIPPED",
    }
    for item in manifest_items:
        if item.source_path in manifest_statuses:
            await store.aupdate_transfer_manifest_item(
                background_records[0].id,
                item.id,
                status=manifest_statuses[item.source_path],
            )

    settings = Settings(app_secret_key="background-list-test-secret", session_cookie_secure=False)
    app = create_app(settings, store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200, login.text
        background_response = await client.get(
            "/api/v1/transfers?background_only=true&offset=0&page_size=200"
        )
        paged_response = await client.get(
            "/api/v1/transfers?background_only=true&offset=2&page_size=2"
        )
        general_response = await client.get(
            "/api/v1/transfers?exclude_background=true&page_size=200"
        )

    assert background_response.status_code == 200
    background_list = background_response.json()
    assert background_list["total_count"] == len(background_specs)
    assert {item["status"] for item in background_list["items"]} == {
        status for status, _metadata in background_specs
    }
    assert foreign_scheduled.id not in {uuid.UUID(item["id"]) for item in background_list["items"]}
    first_background = next(
        item for item in background_list["items"] if item["id"] == str(background_records[0].id)
    )
    assert first_background["manifest_summary"] == {
        "copied": 1,
        "failed": 1,
        "skipped": 1,
        "pending": 1,
        "renamed": 0,
        "omitted": 0,
        "omitted_bytes": 0,
    }
    assert paged_response.json()["total_count"] == len(background_specs)
    assert len(paged_response.json()["items"]) == 2
    general_list = general_response.json()
    assert general_list["total_count"] == len(terminal_records) + 1
    assert {uuid.UUID(item["id"]) for item in general_list["items"]} == {
        record.id for record in terminal_records
    } | {ordinary_queued.id}


@pytest.mark.asyncio
async def test_scheduled_transfer_can_be_reprogrammed_and_cancelled_from_the_api():
    from datetime import timedelta

    store = MemoryStore()
    user = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("CorrectHorseBatteryStaple123!"),
        role="ADMIN",
    )
    source = store.create_connection("Origen", "MOCK", user.id)
    destination = store.create_connection("Destino", "MOCK", user.id)
    app = create_app(
        Settings(app_secret_key="scheduling-test-secret", session_cookie_secure=False),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={
                "username_or_email": "admin",
                "password": "CorrectHorseBatteryStaple123!",
            },
            headers={"X-CSRF-Token": csrf},
        )
        headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
        scheduled_at = datetime.now(UTC) + timedelta(hours=1)
        created = await client.post(
            "/api/v1/transfers",
            json={
                "source_connection_id": str(source.id),
                "destination_connection_id": str(destination.id),
                "source_paths": ["/Backups"],
                "destination_path": "/Cloud",
                "scheduled_at": scheduled_at.isoformat(),
                "schedule_timezone": "America/Santiago",
                "execution_mode": "EXCLUSIVE",
            },
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        transfer_id = created.json()["id"]
        completed = store.create_transfer(
            user.id, source.id, destination.id, ["/Previous"], "/Cloud"
        )
        completed.status = "COMPLETED"
        pending_list = await client.get("/api/v1/transfers?pending_only=true&page_size=200")
        history_list = await client.get("/api/v1/transfers?page_size=200")
        updated_at = datetime.now(UTC) + timedelta(hours=2)
        updated = await client.patch(
            f"/api/v1/transfers/{transfer_id}",
            json={
                "scheduled_at": updated_at.isoformat(),
                "schedule_timezone": "UTC",
                "execution_mode": "AUTO",
                "depends_on_transfer_id": None,
            },
            headers=headers,
        )
        cancelled = await client.post(
            f"/api/v1/transfers/{transfer_id}/cancel", headers=headers
        )
        capabilities = await client.get("/api/v1/transfers/capabilities")

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "SCHEDULED"
    assert [item["id"] for item in pending_list.json()["items"]] == [transfer_id]
    assert len(history_list.json()["items"]) == 2
    assert updated.status_code == 200, updated.text
    assert updated.json()["execution_mode"] == "AUTO"
    assert updated.json()["schedule_timezone"] == "UTC"
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert capabilities.status_code == 200
    assert capabilities.json()["recurrence"] is False

