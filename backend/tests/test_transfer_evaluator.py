from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.services.memory_store import MemoryStore
from app.services.transfer_evaluator import TransferEvaluator
from app.settings import Settings


class FakeSource:
    def __init__(self, file_count: int = 2, failure: Exception | None = None) -> None:
        self.root = StorageItem(
            name="root", path="/root", is_dir=True, provider_type="MOCK", object_id="root"
        )
        self.files = [
            StorageItem(
                name=f"{index}.dat",
                path=f"/root/{index}.dat",
                is_dir=False,
                size_bytes=10,
                provider_type="MOCK",
                object_id=f"file-{index}",
            )
            for index in range(file_count)
        ]
        self.failure = failure

    async def get_item(self, path, **_kwargs):
        return self.root if path == "/root" else None

    async def list_items(self, path, **_kwargs):
        if self.failure is not None:
            raise self.failure
        assert path == "/root"
        return PaginatedItems(items=self.files)


def scheduled_store(*, max_files: int = 5000):
    store = MemoryStore(Settings(transfer_max_files=max_files))
    user = store.add_user("evaluator", "evaluator@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(
        user.id,
        source.id,
        destination.id,
        ["/root"],
        "/archive",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert transfer.evaluation is not None
    return store, transfer, source


@pytest.mark.asyncio
async def test_evaluator_approves_without_changing_copy_state_or_metrics():
    store, transfer, source = scheduled_store(max_files=2)
    provider = FakeSource(file_count=2)
    published = []
    now = datetime.now(UTC)

    async def publish(record):
        published.append(record.public_dict())

    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(transfer_max_files=2),
        store,
        publish,
        provider_factory=lambda _connection: provider,
        now_fn=lambda: now,
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "APPROVED"
    assert current.evaluation.files_found == 2
    assert current.evaluation.count_complete is True
    assert current.status == "SCHEDULED"
    assert current.files_total == 0
    assert current.files_completed == 0
    assert current.bytes_transferred == 0
    assert published[-1]["evaluation"]["status"] == "APPROVED"
    assert published[-1]["files_total"] == 0
    assert source.credential_version == 1


@pytest.mark.asyncio
async def test_evaluator_blocks_at_limit_plus_one_without_failing_the_transfer():
    store, transfer, _source = scheduled_store(max_files=2)
    provider = FakeSource(file_count=3)
    now = datetime.now(UTC)

    async def publish(_record):
        return None

    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(transfer_max_files=2),
        store,
        publish,
        provider_factory=lambda _connection: provider,
        now_fn=lambda: now,
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "BLOCKED"
    assert current.evaluation.files_found == 3
    assert current.evaluation.count_complete is False
    assert current.evaluation.safe_error_code == "TRANSFER_FILE_LIMIT_EXCEEDED"
    assert current.status == "SCHEDULED"
    assert current.error_count == 0


@pytest.mark.asyncio
async def test_evaluator_retries_transient_failures_after_sixty_and_one_twenty_seconds():
    store, transfer, _source = scheduled_store()
    provider = FakeSource(failure=OSError("private provider detail"))
    now = [datetime.now(UTC)]

    async def publish(_record):
        return None

    evaluator = TransferEvaluator(
        Settings(),
        store,
        publish,
        provider_factory=lambda _connection: provider,
        now_fn=lambda: now[0],
    )
    claim = (await store.aclaim_due_evaluations(now[0]))[0]

    await evaluator.execute(claim)
    first = await store.aget_transfer(transfer.id)
    assert first is not None and first.evaluation is not None
    assert first.evaluation.status == "PENDING"
    assert first.evaluation.retry_at == now[0] + timedelta(seconds=60)
    assert "private provider detail" not in (first.evaluation.safe_message or "")

    now[0] += timedelta(seconds=60)
    second_claim = (await store.aclaim_due_evaluations(now[0]))[0]
    await evaluator.execute(second_claim)
    second = await store.aget_transfer(transfer.id)
    assert second is not None and second.evaluation is not None
    assert second.evaluation.status == "PENDING"
    assert second.evaluation.retry_at == now[0] + timedelta(seconds=120)

    now[0] += timedelta(seconds=120)
    third_claim = (await store.aclaim_due_evaluations(now[0]))[0]
    await evaluator.execute(third_claim)
    third = await store.aget_transfer(transfer.id)

    assert third is not None and third.evaluation is not None
    assert third.evaluation.status == "ERROR"
    assert third.evaluation.attempt_count == 3
    assert third.evaluation.retry_at is None
    assert third.status == "SCHEDULED"


@pytest.mark.asyncio
async def test_evaluator_timeout_is_retried_and_heartbeat_is_independent_of_progress():
    store, transfer, _source = scheduled_store()
    heartbeat_calls = 0
    original_heartbeat = store.aheartbeat_evaluation

    async def count_heartbeat(*args, **kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return await original_heartbeat(*args, **kwargs)

    store.aheartbeat_evaluation = count_heartbeat

    class SlowSource(FakeSource):
        async def list_items(self, path, **kwargs):
            await asyncio.sleep(0.05)
            return await super().list_items(path, **kwargs)

    provider = SlowSource()
    now = datetime.now(UTC)

    async def publish(_record):
        return None

    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(),
        store,
        publish,
        provider_factory=lambda _connection: provider,
        now_fn=lambda: now,
        timeout_seconds=0.01,
        heartbeat_seconds=0.001,
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "PENDING"
    assert current.evaluation.safe_error_code == "EVALUATION_TIMEOUT"
    assert heartbeat_calls > 0


@pytest.mark.asyncio
async def test_cancellation_during_listing_does_not_finish_the_old_evaluation_claim():
    store, transfer, _source = scheduled_store()
    now = datetime.now(UTC)

    class CancellingSource(FakeSource):
        async def list_items(self, path, **kwargs):
            await store.acancel_transfer(transfer.id, now)
            return await super().list_items(path, **kwargs)

    async def publish(_record):
        return None

    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(),
        store,
        publish,
        provider_factory=lambda _connection: CancellingSource(),
        now_fn=lambda: now,
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert current is not None and current.evaluation is not None
    assert current.status == "CANCELLED"
    assert current.evaluation.status == "EVALUATING"
    assert current.evaluation.files_found is None


@pytest.mark.asyncio
async def test_mega_session_is_closed_after_successful_evaluation():
    store, transfer, source_connection = scheduled_store()
    destination = store.get_connection(transfer.destination_connection_id)
    assert destination is not None
    source_connection.provider_type = "MEGA"
    destination.provider_type = "GOOGLE_DRIVE"
    provider = FakeSource(file_count=1)
    session_closed = False

    class MegaProvider:
        @asynccontextmanager
        async def open_session(self, **_kwargs):
            nonlocal session_closed
            try:
                yield provider
            finally:
                session_closed = True

    async def publish(_record):
        return None

    now = datetime.now(UTC)
    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(),
        store,
        publish,
        provider_factory=lambda _connection: MegaProvider(),
        now_fn=lambda: now,
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert session_closed is True
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "APPROVED"


@pytest.mark.asyncio
async def test_partial_counts_are_persisted_at_most_once_per_second_plus_final():
    store, transfer, _source = scheduled_store(max_files=10)
    provider = FakeSource(file_count=5)
    now = datetime.now(UTC)
    save_count = 0
    save_progress = store.asave_evaluation_progress

    async def count_save(*args, **kwargs):
        nonlocal save_count
        save_count += 1
        return await save_progress(*args, **kwargs)

    store.asave_evaluation_progress = count_save

    async def publish(_record):
        raise ConnectionError("Redis unavailable")

    claim = (await store.aclaim_due_evaluations(now))[0]
    monotonic_values = iter([0.0, 0.25, 0.5, 0.75, 1.0, 1.25])
    evaluator = TransferEvaluator(
        Settings(transfer_max_files=10),
        store,
        publish,
        provider_factory=lambda _connection: provider,
        now_fn=lambda: now,
        monotonic_fn=lambda: next(monotonic_values),
    )

    await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert save_count == 3
    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "APPROVED"
    assert current.evaluation.files_found == 5
    assert current.evaluation.count_complete is True


@pytest.mark.asyncio
async def test_redelivered_evaluation_waits_for_existing_transfer_lock():
    store, transfer, _source = scheduled_store()
    now = datetime.now(UTC)

    async def publish(_record):
        return None

    claim = (await store.aclaim_due_evaluations(now))[0]
    evaluator = TransferEvaluator(
        Settings(),
        store,
        publish,
        provider_factory=lambda _connection: pytest.fail(
            "duplicate evaluation must not reach the provider"
        ),
        now_fn=lambda: now,
    )

    async with store.transfer_execution_lock(transfer.id) as acquired:
        assert acquired is True
        await evaluator.execute(claim)
    current = await store.aget_transfer(transfer.id)

    assert current is not None and current.evaluation is not None
    assert current.evaluation.status == "EVALUATING"
    assert current.evaluation.claim_token == claim.claim_token
