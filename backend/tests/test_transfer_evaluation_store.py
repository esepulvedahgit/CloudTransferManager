import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select

from app.models import StorageConnection, TransferEvaluation, TransferJob, User
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.services.source_inventory import InventoryProgress
from app.services.transfer_evaluation import EvaluationRecord
from app.settings import Settings


def test_evaluation_public_payload_omits_internal_claim_and_credentials():
    evaluation = EvaluationRecord(
        id=uuid.uuid4(),
        transfer_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        status="EVALUATING",
        files_found=3200,
        count_complete=False,
        max_files=5000,
    )

    public = evaluation.public_dict()

    assert public == {
        "status": "EVALUATING",
        "generation": 1,
        "sequence": 0,
        "files_found": 3200,
        "count_complete": False,
        "max_files": 5000,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "safe_error_code": None,
        "safe_message": None,
    }
    assert "claim_token" not in public
    assert "credentials" not in public


@pytest.mark.asyncio
async def test_scheduled_creation_persists_pending_evaluation_atomically_and_replays(tmp_path):
    store = DatabaseStore(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'eval.sqlite'}"))
    await store.create_schema()
    user = await store.acreate_user("scheduled-owner", "scheduled@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    idempotency_key = uuid.uuid4()
    request_hash = "a" * 64
    scheduled_at = datetime.now(UTC) + timedelta(hours=1)

    first = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=scheduled_at,
        idempotency_key=idempotency_key,
        idempotency_request_hash=request_hash,
    )
    replay = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=scheduled_at,
        idempotency_key=idempotency_key,
        idempotency_request_hash=request_hash,
    )
    persisted = await store.aget_transfer(first.id)
    async with store.sessions() as session:
        rows = list((await session.scalars(select(TransferEvaluation))).all())
    await store.aclose()

    assert replay.id == first.id
    assert replay.idempotent_replay is True
    assert first.evaluation is not None and first.evaluation.status == "PENDING"
    assert persisted is not None and persisted.evaluation is not None
    assert persisted.evaluation.status == "PENDING"
    assert len(rows) == 1
    assert rows[0].transfer_job_id == first.id


@pytest.mark.asyncio
async def test_immediate_transfer_is_not_required_and_scheduled_memory_transfer_is_pending():
    memory = MemoryStore()
    user = memory.add_user("memory-owner", "memory@example.test", "hash")
    source = memory.create_connection("Source", "MOCK", user.id)
    destination = memory.create_connection("Destination", "MOCK", user.id)

    immediate = memory.create_transfer(
        user.id, source.id, destination.id, ["/file"], "/archive"
    )
    scheduled = memory.create_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert immediate.public_dict()["evaluation"]["status"] == "NOT_REQUIRED"
    assert scheduled.evaluation is not None
    assert scheduled.public_dict()["evaluation"]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_evaluation_batch_lookup_uses_one_query_for_many_ids(tmp_path):
    store = DatabaseStore(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'batch.sqlite'}"))
    await store.create_schema()
    user = await store.acreate_user("batch-owner", "batch@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfers = [
        await store.acreate_transfer(
            user.id,
            source.id,
            destination.id,
            [f"/folder-{index}"],
            "/archive",
            scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        )
        for index in range(5)
    ]
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "transfer_evaluations" in statement.lower():
            statements.append(statement)

    event.listen(store.engine.sync_engine, "before_cursor_execute", capture)
    try:
        evaluations = await store.alist_transfer_evaluations([item.id for item in transfers])
    finally:
        event.remove(store.engine.sync_engine, "before_cursor_execute", capture)
        await store.aclose()

    assert set(evaluations) == {item.id for item in transfers}
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_expired_evaluation_claim_is_recovered_and_old_claim_is_fenced(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'claims.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("claim-owner", "claim@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    first_time = datetime.now(UTC)

    first_claims = await store.aclaim_due_evaluations(first_time)
    assert len(first_claims) == 1
    first = first_claims[0]
    second_before_expiry = await store.aclaim_due_evaluations(
        first_time + timedelta(seconds=59)
    )
    assert second_before_expiry == []

    recovered_claims = await store.aclaim_due_evaluations(
        first_time + timedelta(seconds=61)
    )
    assert len(recovered_claims) == 1
    recovered = recovered_claims[0]
    assert recovered.transfer_id == transfer.id
    assert recovered.generation == first.generation
    assert recovered.claim_token != first.claim_token
    assert await store.aheartbeat_evaluation(first, first_time + timedelta(seconds=62)) is False
    assert await store.aheartbeat_evaluation(recovered, first_time + timedelta(seconds=62)) is True
    assert await store.asave_evaluation_progress(
        first, InventoryProgress(files_found=1, count_complete=False), first_time
    ) is False
    assert await store.asave_evaluation_progress(
        recovered, InventoryProgress(files_found=2, count_complete=False), first_time
    ) is True
    assert await store.afinish_evaluation(
        recovered,
        status="APPROVED",
        progress=InventoryProgress(files_found=2, count_complete=True),
        error_code=None,
        message=None,
        now=first_time + timedelta(seconds=63),
    ) is True
    assert await store.afinish_evaluation(
        first,
        status="BLOCKED",
        progress=InventoryProgress(files_found=5001, count_complete=False),
        error_code="TRANSFER_FILE_LIMIT_EXCEEDED",
        message="Divida la selección.",
        now=first_time + timedelta(seconds=64),
    ) is False
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "APPROVED"
    assert current.evaluation.files_found == 2
    assert current.evaluation.count_complete is True
    assert current.wait_reason == "WAITING_UNTIL"


@pytest.mark.asyncio
async def test_evaluation_retry_delays_are_durable_and_attempt_count_is_bounded(tmp_path):
    store = DatabaseStore(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'retry.sqlite'}"))
    await store.create_schema()
    user = await store.acreate_user("retry-owner", "retry@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    start = datetime.now(UTC)

    first = (await store.aclaim_due_evaluations(start))[0]
    assert await store.afinish_evaluation(
        first,
        status="PENDING",
        progress=InventoryProgress(files_found=7, count_complete=False),
        error_code="EVALUATION_PROVIDER_UNAVAILABLE",
        message="No se pudo consultar el proveedor.",
        retry_at=start + timedelta(seconds=60),
        now=start,
    ) is True
    assert await store.aclaim_due_evaluations(start + timedelta(seconds=59)) == []

    second = (await store.aclaim_due_evaluations(start + timedelta(seconds=60)))[0]
    assert second.claim_token != first.claim_token
    assert await store.afinish_evaluation(
        second,
        status="PENDING",
        progress=InventoryProgress(files_found=9, count_complete=False),
        error_code="EVALUATION_PROVIDER_UNAVAILABLE",
        message="No se pudo consultar el proveedor.",
        retry_at=start + timedelta(seconds=180),
        now=start + timedelta(seconds=60),
    ) is True
    assert await store.aclaim_due_evaluations(start + timedelta(seconds=179)) == []

    third = (await store.aclaim_due_evaluations(start + timedelta(seconds=180)))[0]
    assert await store.afinish_evaluation(
        third,
        status="ERROR",
        progress=InventoryProgress(files_found=9, count_complete=False),
        error_code="EVALUATION_PROVIDER_UNAVAILABLE",
        message="No se pudo evaluar tras tres intentos.",
        now=start + timedelta(seconds=180),
    ) is True
    assert await store.aclaim_due_evaluations(start + timedelta(days=1)) == []
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "ERROR"
    assert current.evaluation.attempt_count == 3
    assert current.evaluation.files_found == 9


@pytest.mark.asyncio
async def test_cancelled_transfer_rejects_late_evaluation_progress(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("cancel-owner", "cancel@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    claim = (await store.aclaim_due_evaluations(datetime.now(UTC)))[0]
    await store.acancel_transfer(transfer.id, datetime.now(UTC))

    saved = await store.asave_evaluation_progress(
        claim, InventoryProgress(files_found=42, count_complete=False), datetime.now(UTC)
    )
    current = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert saved is False
    assert current is not None and current.status == "CANCELLED"
    assert current.evaluation is not None and current.evaluation.status == "EVALUATING"


@pytest.mark.asyncio
async def test_evaluation_waits_for_parent_before_claiming_and_wakes_after_success(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'parent-evaluation.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("parent-owner", "parent@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/parent"], "/archive"
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/child"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        depends_on_transfer_id=parent.id,
    )
    now = datetime.now(UTC)

    waiting_claims = await store.aclaim_due_evaluations(now)
    waiting = await store.aget_transfer(child.id)
    parent.status = "COMPLETED_WITH_WARNINGS"
    await store.asave_transfer(parent)
    approved_parent_claim = await store.aclaim_due_evaluations(now + timedelta(seconds=1))
    await store.aclose()

    assert waiting_claims == []
    assert waiting is not None and waiting.evaluation is not None
    assert waiting.evaluation.status == "WAITING_PARENT"
    assert waiting.wait_reason == "WAITING_PARENT"
    assert len(approved_parent_claim) == 1
    assert approved_parent_claim[0].transfer_id == child.id


@pytest.mark.asyncio
async def test_failed_parent_keeps_child_blocked_without_claiming_evaluation(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'failed-parent-evaluation.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("failed-parent-owner", "failed-parent@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    parent = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/parent"], "/archive"
    )
    parent.status = "FAILED"
    await store.asave_transfer(parent)
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/child"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        depends_on_transfer_id=parent.id,
    )

    claims = await store.aclaim_due_evaluations(datetime.now(UTC))
    current = await store.aget_transfer(child.id)
    await store.aclose()

    assert claims == []
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "WAITING_PARENT"
    assert current.evaluation.safe_error_code == "BLOCKED_PARENT_FAILED"
    assert current.wait_reason == "BLOCKED_PARENT_FAILED"


@pytest.mark.asyncio
async def test_evaluation_claim_checks_connection_access_and_credential_versions(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'evaluation-access.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("access-owner", "access@example.test", "hash")
    source = await store.acreate_connection(
        "Private source", "MOCK", user.id, uuid.uuid4(), b"", is_global=False
    )
    destination = await store.acreate_connection(
        "Destination", "MOCK", user.id, uuid.uuid4(), b""
    )
    child = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )

    access_claims = await store.aclaim_due_evaluations(datetime.now(UTC))
    access_blocked = await store.aget_transfer(child.id)
    assert access_claims == []
    assert access_blocked is not None and access_blocked.wait_reason == "WAITING_CONNECTION_ACCESS"

    from sqlalchemy import update

    from app.models import StorageConnection

    await store.agrant_connection(user.id, source.id, user.id)
    async with store.sessions() as session:
        await session.execute(
            update(StorageConnection)
            .where(StorageConnection.id == source.id)
            .values(credential_version=StorageConnection.credential_version + 1)
        )
        await session.commit()

    # A grant permits another claim attempt; the stale transfer snapshot blocks first.
    claims_after_credential_change = await store.aclaim_due_evaluations(datetime.now(UTC))
    current = await store.aget_transfer(child.id)
    await store.aclose()

    assert claims_after_credential_change == []
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "BLOCKED"
    assert current.evaluation.safe_error_code == "BLOCKED_CONNECTION_CHANGED"


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
@pytest.mark.asyncio
async def test_postgres_evaluation_reservation_is_exclusive_and_recovery_respects_live_lock():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"])
    first_store = DatabaseStore(settings)
    second_store = DatabaseStore(settings)
    await first_store.create_schema()
    async with first_store.sessions() as session:
        active_count = await session.scalar(
            select(func.count(TransferEvaluation.id)).where(
                TransferEvaluation.status.in_({"PENDING", "EVALUATING"})
            )
        )
    if active_count:
        await first_store.aclose()
        await second_store.aclose()
        pytest.skip("integration DB already contains an active evaluation")

    user = await first_store.acreate_user(
        f"eval-{uuid.uuid4().hex[:10]}", f"eval-{uuid.uuid4().hex}@example.test", "hash"
    )
    source = await first_store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await first_store.acreate_connection(
        "Destination", "MOCK", user.id, uuid.uuid4(), b""
    )
    transfer = await first_store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/folder"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    now = datetime.now(UTC)
    first_claims, second_claims = await asyncio.gather(
        first_store.aclaim_due_evaluations(now),
        second_store.aclaim_due_evaluations(now),
    )
    claim_list = first_claims + second_claims
    assert len(claim_list) == 1
    original_claim = claim_list[0]

    async with first_store.transfer_execution_lock(transfer.id) as acquired:
        assert acquired is True
        assert (
            await second_store.aclaim_due_evaluations(now + timedelta(seconds=61))
            == []
        )

    recovered_claims = await second_store.aclaim_due_evaluations(
        now + timedelta(seconds=61)
    )
    assert len(recovered_claims) == 1
    assert recovered_claims[0].claim_token != original_claim.claim_token
    assert await first_store.aheartbeat_evaluation(
        original_claim, now + timedelta(seconds=62)
    ) is False

    async with first_store.sessions() as session:
        async with session.begin():
            await session.execute(delete(TransferJob).where(TransferJob.id == transfer.id))
            await session.execute(
                delete(StorageConnection).where(
                    StorageConnection.id.in_([source.id, destination.id])
                )
            )
            await session.execute(delete(User).where(User.id == user.id))
    await first_store.aclose()
    await second_store.aclose()
