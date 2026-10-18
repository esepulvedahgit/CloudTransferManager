import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.models import StorageConnection, TransferJob, User
from app.services.database_store import DatabaseStore
from app.settings import Settings


@pytest.mark.asyncio
async def test_database_pause_fences_stale_worker_and_resumes_without_new_attempt(tmp_path):
    store = DatabaseStore(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'pause.sqlite'}"))
    await store.create_schema()
    user = await store.acreate_user("pause-db", "pause-db@example.test", "hash", "ADMIN")
    source = await store.acreate_connection("MOCK origen", "MOCK", user.id, uuid.uuid4(), b"s")
    destination = await store.acreate_connection(
        "MOCK destino", "MOCK", user.id, uuid.uuid4(), b"d"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    token = uuid.uuid4()
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                update(TransferJob)
                .where(TransferJob.id == transfer.id)
                .values(
                    status="RUNNING", dispatch_token=token, execution_started_at=datetime.now(UTC)
                )
            )

    try:
        requested, needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))
        stale = await store.aget_transfer(transfer.id)
        stale.status = "RUNNING"
        stale.current_item = "/Documents/current.bin"
        await store.asave_transfer(stale)
        checkpoint = await store.acheck_transfer_execution_control(
            transfer.id, token, "/Documents/next.bin"
        )
        paused = await store.acomplete_transfer_pause(transfer.id, token, datetime.now(UTC))
        attempt_before_resume = paused.attempt_count
        resumed = await store.aresume_transfer(transfer.id)
    finally:
        await store.aclose()

    assert requested is not None and requested.status == "PAUSING"
    assert needs_worker is True
    assert stale.status == "PAUSING"
    assert checkpoint == "pause"
    assert paused is not None and paused.status == "PAUSED"
    assert resumed is not None and resumed.status == "QUEUED"
    assert resumed.attempt_count == attempt_before_resume
    assert resumed.state_version > paused.state_version


@pytest.mark.asyncio
async def test_database_pause_unstarted_future_schedule_preserves_schedule_on_resume(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'scheduled-pause.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user(
        "pause-schedule-db", "pause-schedule-db@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("MOCK origen", "MOCK", user.id, uuid.uuid4(), b"s")
    destination = await store.acreate_connection(
        "MOCK destino", "MOCK", user.id, uuid.uuid4(), b"d"
    )
    scheduled_at = datetime.now(UTC) + timedelta(days=2)
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/Documents"],
        "/Backup",
        scheduled_at=scheduled_at,
    )

    try:
        paused, needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))
        resumed = await store.aresume_transfer(transfer.id)
    finally:
        await store.aclose()

    assert paused is not None and paused.status == "PAUSED"
    assert needs_worker is False
    assert resumed is not None and resumed.status == "SCHEDULED"
    assert resumed.scheduled_at.replace(tzinfo=UTC) == scheduled_at


@pytest.mark.asyncio
async def test_database_rechecks_boundary_support_after_queued_job_is_dispatched(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-race.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("legacy-pause", "legacy-pause@example.test", "hash", "ADMIN")
    source = await store.acreate_connection("MEGA origen", "MEGA", user.id, uuid.uuid4(), b"s")
    destination = await store.acreate_connection(
        "Drive destino", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"d"
    )
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/Documents"],
        "/Backup",
        conflict_policy="SKIP",
    )
    stale_view = await store.aget_transfer(transfer.id)
    token = uuid.uuid4()
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                update(TransferJob)
                .where(TransferJob.id == transfer.id)
                .values(
                    status="QUEUED",
                    execution_started_at=datetime.now(UTC),
                    dispatch_token=token,
                )
            )

    try:
        paused, needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))
        persisted = await store.aget_transfer(transfer.id)
    finally:
        await store.aclose()

    assert stale_view.status == "QUEUED"
    assert paused is None
    assert needs_worker is False
    assert persisted.status == "QUEUED"
    assert persisted.pause_requested_at is None


@pytest.mark.asyncio
async def test_database_rejects_progress_save_from_replaced_dispatch_token(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'stale-token.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user(
        "stale-progress", "stale-progress@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("MOCK origen", "MOCK", user.id, uuid.uuid4(), b"s")
    destination = await store.acreate_connection(
        "MOCK destino", "MOCK", user.id, uuid.uuid4(), b"d"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    old_token = uuid.uuid4()
    replacement_token = uuid.uuid4()
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                update(TransferJob)
                .where(TransferJob.id == transfer.id)
                .values(status="RUNNING", dispatch_token=old_token)
            )

    try:
        stale = await store.aget_transfer(transfer.id)
        stale.bytes_transferred = 999
        async with store.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(TransferJob)
                    .where(TransferJob.id == transfer.id)
                    .values(dispatch_token=replacement_token, bytes_transferred=12)
                )
        accepted = await store.asave_transfer(stale, expected_dispatch_token=old_token)
        persisted = await store.aget_transfer(transfer.id)
    finally:
        await store.aclose()

    assert accepted is False
    assert persisted.dispatch_token == replacement_token
    assert persisted.bytes_transferred == 12


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("CTM_TEST_POSTGRES_URL"), reason="requires PostgreSQL integration DB"
)
async def test_postgres_serializes_pause_with_next_file_checkpoint():
    store = DatabaseStore(Settings(database_url=os.environ["CTM_TEST_POSTGRES_URL"]))
    assert store.engine.dialect.name == "postgresql"
    await store.create_schema()
    suffix = uuid.uuid4().hex
    user = await store.acreate_user(
        f"pause-race-{suffix}", f"pause-race-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection(
        "Pause race source", "MOCK", user.id, uuid.uuid4(), b""
    )
    destination = await store.acreate_connection(
        "Pause race destination", "MOCK", user.id, uuid.uuid4(), b""
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/file"], "/archive"
    )
    dispatch_token = uuid.uuid4()
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                update(TransferJob)
                .where(TransferJob.id == transfer.id)
                .values(
                    status="RUNNING",
                    dispatch_token=dispatch_token,
                    execution_started_at=datetime.now(UTC),
                )
            )

    try:
        pause_result, checkpoint = await asyncio.gather(
            store.apause_transfer(transfer.id, datetime.now(UTC)),
            store.acheck_transfer_execution_control(
                transfer.id, dispatch_token, "/next-file"
            ),
        )
        persisted = await store.aget_transfer(transfer.id)
        stale_progress = await store.aget_transfer(transfer.id)
        assert stale_progress is not None
        stale_progress.bytes_transferred = 999
        replacement_token = uuid.uuid4()
        async with store.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(TransferJob)
                    .where(TransferJob.id == transfer.id)
                    .values(dispatch_token=replacement_token, bytes_transferred=12)
                )
        accepted_stale_progress = await store.asave_transfer(
            stale_progress, expected_dispatch_token=dispatch_token
        )
        fenced = await store.aget_transfer(transfer.id)
    finally:
        async with store.sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(TransferJob).where(TransferJob.id == transfer.id)
                )
                await session.execute(
                    delete(StorageConnection).where(
                        StorageConnection.id.in_([source.id, destination.id])
                    )
                )
                await session.execute(delete(User).where(User.id == user.id))
        await store.aclose()

    assert pause_result[0] is not None and pause_result[0].status == "PAUSING"
    assert pause_result[1] is True
    assert persisted is not None and persisted.status == "PAUSING"
    assert accepted_stale_progress is False
    assert fenced is not None
    assert fenced.dispatch_token == replacement_token
    assert fenced.bytes_transferred == 12
    if checkpoint == "continue":
        assert persisted.current_item == "/next-file"
    else:
        assert checkpoint == "pause"
        assert persisted.current_item is None
