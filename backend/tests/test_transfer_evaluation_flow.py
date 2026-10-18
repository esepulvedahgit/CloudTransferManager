from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.services.provider_factory import StorageProviderFactory
from app.services.transfer_evaluation import EvaluationClaim
from app.services.transfer_evaluator import TransferEvaluator
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings


class PagedSource:
    def __init__(self, file_count: int, *, pause_before_second_page: bool = False) -> None:
        self.root = StorageItem(
            name="root", path="/root", is_dir=True, provider_type="MOCK", object_id="root"
        )
        self.files = [self._file(index) for index in range(file_count)]
        self.pause_before_second_page = pause_before_second_page
        self.second_page_requested = asyncio.Event()
        self.continue_scan = asyncio.Event()
        if not pause_before_second_page:
            self.continue_scan.set()

    @staticmethod
    def _file(index: int) -> StorageItem:
        return StorageItem(
            name=f"{index}.dat",
            path=f"/root/{index}.dat",
            is_dir=False,
            size_bytes=10,
            provider_type="MOCK",
            object_id=f"file-{index}",
        )

    async def get_item(self, path: str, **_kwargs) -> StorageItem | None:
        return self.root if path == "/root" else next(
            (item for item in self.files if item.path == path), None
        )

    async def list_items(self, path: str, cursor: str | None = None, **_kwargs):
        assert path == "/root"
        if cursor is None:
            page = self.files[:1] if self.pause_before_second_page else self.files
            next_cursor = "second" if self.pause_before_second_page else None
            return PaginatedItems(items=page, next_cursor=next_cursor)
        if cursor == "second":
            self.second_page_requested.set()
            await self.continue_scan.wait()
            return PaginatedItems(items=self.files[1:])
        raise AssertionError(f"unexpected cursor: {cursor}")


class StoreHandle:
    """Allow a test to reopen the database while the provider scan is paused."""

    def __init__(self, current: DatabaseStore) -> None:
        self.current = current

    def __getattr__(self, name: str):
        return getattr(self.current, name)


@pytest.mark.asyncio
async def test_evaluator_preserves_parent_omission_block_reason():
    settings = Settings()
    store = MemoryStore(settings)
    user = store.add_user("parent-eval", "parent-eval@example.test", "hash")
    source_connection = store.create_connection(
        "Source", "MOCK", user.id, encrypted_credentials=b""
    )
    destination_connection = store.create_connection(
        "Destination", "MOCK", user.id, encrypted_credentials=b""
    )
    parent = store.create_transfer(
        user.id, source_connection.id, destination_connection.id, ["/parent"], "/destination"
    )
    parent.status = "COMPLETED_WITH_WARNINGS"
    store.transfers[parent.id] = parent
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
    parent_item = (await store.alist_transfer_manifest_items(parent.id))[0]
    parent_item.status = "SKIPPED"
    parent_item.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    parent_item.omitted_at = datetime.now(UTC)

    child = store.create_transfer(
        user.id,
        source_connection.id,
        destination_connection.id,
        ["/child"],
        "/destination",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        depends_on_transfer_id=parent.id,
    )
    store.transfers[child.id] = child
    assert child.evaluation is not None
    claim_token = uuid.uuid4()
    child.evaluation.status = "EVALUATING"
    child.evaluation.claim_token = claim_token
    child.evaluation.generation = 2
    claim = EvaluationClaim(child.id, 2, claim_token)

    async def publish(_record) -> None:
        return None

    evaluator = TransferEvaluator(settings, store, publish)
    await evaluator.execute(claim)
    updated = await store.aget_transfer(child.id)

    assert updated is not None and updated.evaluation is not None
    assert updated.evaluation.status == "WAITING_PARENT"
    assert updated.evaluation.safe_error_code == "BLOCKED_PARENT_OMISSIONS"
    assert updated.wait_reason == "BLOCKED_PARENT_OMISSIONS"


@pytest.mark.asyncio
async def test_over_limit_schedule_persists_partial_count_across_store_reopen_and_never_dispatches(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'evaluation-flow.sqlite'}",
        transfer_max_files=5000,
    )
    store_before_restart = DatabaseStore(settings)
    await store_before_restart.create_schema()
    user = await store_before_restart.acreate_user("flow-user", "flow@example.test", "hash")
    source_connection = await store_before_restart.acreate_connection(
        "Source", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"encrypted"
    )
    destination_connection = await store_before_restart.acreate_connection(
        "Destination", "SEAFILE", user.id, uuid.uuid4(), b"encrypted"
    )
    started_at = datetime.now(UTC)
    scheduled_at = started_at + timedelta(minutes=1)
    transfer = await store_before_restart.acreate_transfer(
        user.id,
        source_connection.id,
        destination_connection.id,
        ["/root"],
        "/destination",
        scheduled_at=scheduled_at,
    )
    claim = (await store_before_restart.aclaim_due_evaluations(started_at))[0]
    source = PagedSource(5001, pause_before_second_page=True)
    handle = StoreHandle(store_before_restart)

    async def publish(_record) -> None:
        return None

    evaluator = TransferEvaluator(
        settings,
        handle,
        publish,
        provider_factory=lambda _connection: source,
        now_fn=lambda: started_at,
        monotonic_fn=lambda: 0.0,
    )
    evaluation_task = asyncio.create_task(evaluator.execute(claim))
    store_after_restart: DatabaseStore | None = None
    try:
        await asyncio.wait_for(source.second_page_requested.wait(), timeout=15)
        partial = await store_before_restart.aget_transfer(transfer.id)
        assert partial is not None and partial.evaluation is not None
        assert partial.evaluation.status == "EVALUATING"
        assert partial.evaluation.files_found == 1
        assert partial.evaluation.count_complete is False

        await store_before_restart.aclose()
        store_after_restart = DatabaseStore(settings)
        handle.current = store_after_restart
        reopened = await store_after_restart.aget_transfer(transfer.id)
        assert reopened is not None and reopened.evaluation is not None
        assert reopened.evaluation.files_found == 1

        source.continue_scan.set()
        await evaluation_task

        blocked = await store_after_restart.aget_transfer(transfer.id)
        assert blocked is not None and blocked.evaluation is not None
        assert blocked.evaluation.status == "BLOCKED"
        assert blocked.evaluation.files_found == 5001
        assert blocked.evaluation.count_complete is False
        assert blocked.status == "SCHEDULED"
        assert blocked.files_total == 0
        assert blocked.dispatch_token is None

        due = await store_after_restart.adispatch_due_transfers(
            scheduled_at + timedelta(seconds=1), max_concurrent=1
        )
        assert due.admitted == []
        still_blocked = await store_after_restart.aget_transfer(transfer.id)
        assert still_blocked is not None and still_blocked.dispatch_token is None
    finally:
        source.continue_scan.set()
        if not evaluation_task.done():
            await evaluation_task
        if store_after_restart is not None:
            await store_after_restart.aclose()
        else:
            await store_before_restart.aclose()


@pytest.mark.asyncio
async def test_exact_limit_approval_dispatches_but_final_preflight_catches_a_grown_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    settings = Settings(
        transfer_max_files=5000,
        transfer_temp_path=str(tmp_path / "rclone-tmp"),
        transfer_staging_path=str(tmp_path / "staging"),
    )
    store = MemoryStore(settings)
    user = store.add_user("limit-user", "limit@example.test", "hash")
    source_connection = store.create_connection(
        "Source", "GOOGLE_DRIVE", user.id, encrypted_credentials=b"encrypted"
    )
    destination_connection = store.create_connection(
        "Destination", "SEAFILE", user.id, encrypted_credentials=b"encrypted"
    )
    started_at = datetime.now(UTC)
    scheduled_at = started_at + timedelta(minutes=1)
    transfer = store.create_transfer(
        user.id,
        source_connection.id,
        destination_connection.id,
        ["/root"],
        "/destination",
        conflict_policy="SKIP",
        scheduled_at=scheduled_at,
    )
    source = PagedSource(5000)

    async def publish(record) -> None:
        await store.asave_transfer(record)

    claim = (await store.aclaim_due_evaluations(started_at))[0]
    evaluator = TransferEvaluator(
        settings,
        store,
        publish,
        provider_factory=lambda _connection: source,
        now_fn=lambda: started_at,
    )
    await evaluator.execute(claim)
    approved = await store.aget_transfer(transfer.id)
    assert approved is not None and approved.evaluation is not None
    assert approved.evaluation.status == "APPROVED"
    assert approved.evaluation.files_found == 5000
    assert approved.evaluation.count_complete is True

    admitted = await store.adispatch_due_transfers(
        scheduled_at + timedelta(seconds=1), max_concurrent=1
    )
    assert [item.id for item in admitted.admitted] == [transfer.id]
    assert admitted.admitted[0].dispatch_token is not None

    source.files.append(source._file(5000))

    class UntouchedDestination:
        def __init__(self) -> None:
            self.calls = 0

        async def get_item(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("destination must not be queried after failed preflight")

    destination = UntouchedDestination()

    def fake_factory_create(_factory, connection, *, runner=None):
        del runner
        return source if connection.id == source_connection.id else destination

    monkeypatch.setattr(StorageProviderFactory, "create", fake_factory_create)

    class RecordingRunner:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("copy command must not start after failed preflight")

    runner = RecordingRunner()
    executor = TransferExecutor(
        settings,
        store,
        publish,
        should_cancel=lambda: asyncio.sleep(0, result=False),
        runner=runner,
    )
    await executor.execute(transfer.id)

    final = await store.aget_transfer(transfer.id)
    events = await store.alist_transfer_events(transfer.id)
    assert final is not None and final.status == "FAILED"
    assert runner.calls == []
    assert destination.calls == 0
    assert any(event["provider_error_code"] == "TRANSFER_FILE_LIMIT_EXCEEDED" for event in events)


@pytest.mark.asyncio
async def test_partial_evaluation_advances_while_an_unrelated_copy_lock_is_held():
    settings = Settings(transfer_max_files=10)
    store = MemoryStore(settings)
    user = store.add_user("independent-user", "independent@example.test", "hash")
    active_source = store.create_connection(
        "Active source", "GOOGLE_DRIVE", user.id, encrypted_credentials=b"encrypted"
    )
    active_destination = store.create_connection(
        "Active destination", "SEAFILE", user.id, encrypted_credentials=b"encrypted"
    )
    scheduled_source = store.create_connection(
        "Scheduled source", "GOOGLE_DRIVE", user.id, encrypted_credentials=b"encrypted"
    )
    scheduled_destination = store.create_connection(
        "Scheduled destination", "SEAFILE", user.id, encrypted_credentials=b"encrypted"
    )
    started_at = datetime.now(UTC)
    scheduled_at = started_at + timedelta(minutes=1)
    active_copy = store.create_transfer(
        user.id,
        active_source.id,
        active_destination.id,
        ["/root"],
        "/active",
        conflict_policy="SKIP",
    )
    scheduled_copy = store.create_transfer(
        user.id,
        scheduled_source.id,
        scheduled_destination.id,
        ["/root"],
        "/scheduled",
        scheduled_at=scheduled_at,
    )
    reservation = await store.adispatch_due_transfers(started_at, max_concurrent=1)
    assert [item.id for item in reservation.admitted] == [active_copy.id]

    source = PagedSource(2, pause_before_second_page=True)

    async def publish(_record) -> None:
        return None

    claim = (await store.aclaim_due_evaluations(started_at))[0]
    evaluator = TransferEvaluator(
        settings,
        store,
        publish,
        provider_factory=lambda _connection: source,
        now_fn=lambda: started_at,
    )

    async with store.transfer_execution_lock(active_copy.id) as active_lock:
        assert active_lock is True
        evaluation_task = asyncio.create_task(evaluator.execute(claim))
        try:
            await asyncio.wait_for(source.second_page_requested.wait(), timeout=5)
            partial = await store.aget_transfer(scheduled_copy.id)
            assert partial is not None and partial.evaluation is not None
            assert partial.evaluation.status == "EVALUATING"
            assert partial.evaluation.files_found == 1
            source.continue_scan.set()
            await evaluation_task
        finally:
            source.continue_scan.set()
            if not evaluation_task.done():
                await evaluation_task

    approved = await store.aget_transfer(scheduled_copy.id)
    still_running = await store.aget_transfer(active_copy.id)
    assert approved is not None and approved.evaluation is not None
    assert approved.evaluation.status == "APPROVED"
    assert approved.evaluation.files_found == 2
    assert still_running is not None and still_running.dispatch_token is not None


@pytest.mark.asyncio
async def test_failed_evaluation_does_not_hold_up_compatible_copy_while_another_copy_runs():
    settings = Settings(transfer_max_files=10)
    store = MemoryStore(settings)
    user = store.add_user("parallel-user", "parallel@example.test", "hash")

    def connection_pair(label: str):
        source = store.create_connection(
            f"{label} source", "GOOGLE_DRIVE", user.id, encrypted_credentials=b"encrypted"
        )
        destination = store.create_connection(
            f"{label} destination", "SEAFILE", user.id, encrypted_credentials=b"encrypted"
        )
        return source, destination

    active_source, active_destination = connection_pair("active")
    scheduled_source, scheduled_destination = connection_pair("scheduled")
    other_source, other_destination = connection_pair("other")
    started_at = datetime.now(UTC)
    scheduled_at = started_at + timedelta(minutes=1)
    active_copy = store.create_transfer(
        user.id,
        active_source.id,
        active_destination.id,
        ["/root"],
        "/active",
        conflict_policy="SKIP",
    )
    scheduled_exclusive = store.create_transfer(
        user.id,
        scheduled_source.id,
        scheduled_destination.id,
        ["/root"],
        "/scheduled",
        execution_mode="EXCLUSIVE",
        scheduled_at=scheduled_at,
    )
    other_copy = store.create_transfer(
        user.id,
        other_source.id,
        other_destination.id,
        ["/root"],
        "/other",
        conflict_policy="SKIP",
    )

    initial = await store.adispatch_due_transfers(started_at, max_concurrent=1)
    assert [item.id for item in initial.admitted] == [active_copy.id]
    claim = (await store.aclaim_due_evaluations(started_at))[0]

    class UnavailableSource:
        async def get_item(self, *_args, **_kwargs):
            raise OSError("private provider detail")

    async def publish(_record) -> None:
        return None

    evaluator = TransferEvaluator(
        settings,
        store,
        publish,
        provider_factory=lambda _connection: UnavailableSource(),
        now_fn=lambda: started_at,
    )

    async with store.transfer_execution_lock(active_copy.id) as active_lock:
        assert active_lock is True
        await evaluator.execute(claim)
        failed_evaluation = await store.aget_transfer(scheduled_exclusive.id)
        assert failed_evaluation is not None and failed_evaluation.evaluation is not None
        assert failed_evaluation.evaluation.status == "PENDING"
        assert failed_evaluation.wait_reason == "WAITING_EVALUATION"

        later = await store.adispatch_due_transfers(
            scheduled_at + timedelta(seconds=1), max_concurrent=2
        )

    assert [item.id for item in later.admitted] == [other_copy.id], [
        (item.id, item.status, item.wait_reason, item.dispatch_token)
        for item in later.changed
    ]
    still_running = await store.aget_transfer(active_copy.id)
    assert still_running is not None and still_running.dispatch_token is not None
