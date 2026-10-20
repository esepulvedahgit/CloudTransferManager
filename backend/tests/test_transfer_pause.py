import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.services.memory_store import MemoryStore
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings


@pytest.mark.asyncio
async def test_pause_checkpoint_blocks_next_file_then_finalizes_after_worker_cleanup():
    store = MemoryStore()
    user = store.add_user("pause", "pause@example.test", "hash", "ADMIN")
    source = store.create_connection("Mock origen", "MOCK", user.id)
    destination = store.create_connection("Mock destino", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    dispatch_token = transfer.dispatch_token

    requested, needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))
    requested_status = requested.status if requested is not None else None
    requested_version = requested.state_version if requested is not None else 0
    decision = await store.acheck_transfer_execution_control(
        transfer.id, dispatch_token, "/Documents/next.txt"
    )
    finalized = await store.acomplete_transfer_pause(transfer.id, dispatch_token, datetime.now(UTC))

    assert requested is not None and requested_status == "PAUSING"
    assert needs_worker is True
    assert decision == "pause"
    assert finalized is not None and finalized.status == "PAUSED"
    assert finalized.current_item is None
    assert finalized.dispatch_token is None
    assert finalized.paused_at is not None
    assert finalized.state_version > requested_version


@pytest.mark.asyncio
async def test_checkpoint_claim_and_cancel_take_precedence_over_pause():
    store = MemoryStore()
    user = store.add_user("control", "control@example.test", "hash", "ADMIN")
    source = store.create_connection("Mock origen", "MOCK", user.id)
    destination = store.create_connection("Mock destino", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    token = transfer.dispatch_token

    assert (
        await store.acheck_transfer_execution_control(transfer.id, token, "/Documents/current.txt")
    ) == "continue"
    requested, _needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))
    assert requested is not None and requested.status == "PAUSING"
    assert requested.current_item == "/Documents/current.txt"
    await store.acancel_transfer(transfer.id, datetime.now(UTC))

    decision = await store.acheck_transfer_execution_control(
        transfer.id, token, "/Documents/next.txt"
    )

    assert decision == "cancel"
    assert (await store.aget_transfer(transfer.id)).status == "CANCELLING"


@pytest.mark.asyncio
async def test_pause_transition_does_not_replace_a_pending_cancellation():
    store = MemoryStore()
    user = store.add_user("cancel-first", "cancel-first@example.test", "hash", "ADMIN")
    source = store.create_connection("Mock origen", "MOCK", user.id)
    destination = store.create_connection("Mock destino", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    await store.acancel_transfer(transfer.id, datetime.now(UTC))

    paused, needs_worker = await store.apause_transfer(transfer.id, datetime.now(UTC))

    assert paused is None
    assert needs_worker is False
    assert (await store.aget_transfer(transfer.id)).status == "CANCELLING"


@pytest.mark.asyncio
async def test_memory_store_stale_snapshot_cannot_undo_pending_cancellation():
    store = MemoryStore()
    user = store.add_user("cancel-snapshot", "cancel-snapshot@example.test", "hash", "ADMIN")
    source = store.create_connection("Mock origen", "MOCK", user.id)
    destination = store.create_connection("Mock destino", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/file"], "/Backup"
    )
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    stale_snapshot = replace(transfer)

    await store.acancel_transfer(transfer.id, datetime.now(UTC))
    stale_snapshot.status = "RUNNING"
    stale_snapshot.cancel_requested_at = None
    await store.asave_transfer(stale_snapshot)

    persisted = await store.aget_transfer(transfer.id)
    assert persisted.status == "CANCELLING"
    assert persisted.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_mock_executor_finishes_current_file_and_pauses_before_selecting_next():
    store = MemoryStore()
    user = store.add_user("mock-pause", "mock-pause@example.test", "hash", "ADMIN")
    source = store.create_connection("Mock origen", "MOCK", user.id)
    destination = store.create_connection("Mock destino", "MOCK", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Proyectos"], "/Backup"
    )
    transfer.execution_started_at = datetime.now(UTC)
    transfer.dispatch_token = uuid.uuid4()
    await store.asave_transfer(transfer)
    pause_requested = False

    async def publish(record):
        await store.asave_transfer(record)

    async def never_cancel():
        return False

    async def pause_during_current_file(_delay):
        nonlocal pause_requested
        if not pause_requested:
            pause_requested = True
            await store.apause_transfer(transfer.id, datetime.now(UTC))

    executor = TransferExecutor(
        Settings(), store, publish, never_cancel, sleep=pause_during_current_file
    )

    await executor.execute(transfer.id)
    paused = await store.aget_transfer(transfer.id)

    assert paused.status == "PAUSED"
    assert paused.files_completed == 1
    assert paused.progress_percent < 100
    assert paused.current_item is None
