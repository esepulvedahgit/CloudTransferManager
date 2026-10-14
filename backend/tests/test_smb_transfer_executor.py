import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.services.memory_store import ConnectionRecord, MemoryStore, TransferRecord
from app.services.rclone import RcloneCommandError
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings


class LargeSmbListing:
    provider_type = "SMB"

    def __init__(self, file_count: int) -> None:
        self.file_count = file_count
        self.pages: list[int] = []

    async def get_item(self, path: str) -> StorageItem | None:
        if path == "/large":
            return StorageItem(name="large", path="/large", is_dir=True, provider_type="SMB")
        return None

    async def list_items(
        self,
        path: str,
        *,
        cursor: str | None = None,
        page_size: int = 200,
        **_kwargs,
    ) -> PaginatedItems:
        assert path == "/large"
        offset = int(cursor or 0)
        self.pages.append(offset)
        end = min(offset + page_size, self.file_count)
        items = [
            StorageItem(
                name=f"file-{index:05d}.dat",
                path=f"/large/file-{index:05d}.dat",
                is_dir=False,
                size_bytes=1024,
                modified_at=datetime(2026, 1, 1, tzinfo=UTC),
                provider_type="SMB",
            )
            for index in range(offset, end)
        ]
        return PaginatedItems(
            items=items,
            next_cursor=str(end) if end < self.file_count else None,
            total_count=self.file_count,
        )


class SmallSmbTree:
    provider_type = "SMB"

    def __init__(self) -> None:
        self.items = {
            "/NAS": StorageItem(name="NAS", path="/NAS", is_dir=True, provider_type="SMB"),
            "/NAS/Empty": StorageItem(
                name="Empty", path="/NAS/Empty", is_dir=True, provider_type="SMB"
            ),
            "/NAS/report.pdf": StorageItem(
                name="report.pdf",
                path="/NAS/report.pdf",
                is_dir=False,
                size_bytes=7,
                provider_type="SMB",
            ),
        }

    async def get_item(self, path: str) -> StorageItem | None:
        return self.items.get(path)

    async def list_items(self, path: str, cursor=None, page_size=200, **_kwargs):
        children = [item for item in self.items.values() if item.path.rpartition("/")[0] == path]
        return PaginatedItems(items=children, next_cursor=None, total_count=len(children))

    def remote_path(self, path: str) -> str:
        return f"ctm_{'1' * 32}:{path}"

    def config_content(self) -> str:
        return "[smb]\ntype = smb\n"


class MultiFileSmbTree:
    provider_type = "SMB"

    def __init__(self, files: dict[str, int]) -> None:
        self.items = {
            "/NAS": StorageItem(name="NAS", path="/NAS", is_dir=True, provider_type="SMB")
        }
        for name, size in files.items():
            path = f"/NAS/{name}"
            self.items[path] = StorageItem(
                name=name,
                path=path,
                is_dir=False,
                size_bytes=size,
                provider_type="SMB",
            )

    async def get_item(self, path: str) -> StorageItem | None:
        return self.items.get(path)

    async def list_items(self, path: str, cursor=None, page_size=200, **_kwargs):
        children = [item for item in self.items.values() if item.path.rpartition("/")[0] == path]
        return PaginatedItems(items=children, next_cursor=None, total_count=len(children))

    def remote_path(self, path: str) -> str:
        return f"ctm_{'1' * 32}:{path}"

    def config_content(self) -> str:
        return "[smb]\ntype = smb\n"


class FakeCloudDestination:
    provider_type = "GOOGLE_DRIVE"

    def __init__(self) -> None:
        self.items: dict[str, StorageItem] = {}

    def remote_path(self, path: str) -> str:
        return f"ctm_{'2' * 32}:{path}"

    def config_content(self) -> str:
        return "[drive]\ntype = drive\n"

    async def get_item(self, path: str) -> StorageItem | None:
        return self.items.get(path)


class FakeCopyRunner:
    def __init__(self, destination: FakeCloudDestination) -> None:
        self.destination = destination
        self.commands: list[tuple[str, list[str]]] = []
        self.directory_trees: list[list[str]] = []

    async def run(self, command, remote_paths, _config, _job_id, **_kwargs):
        self.commands.append((command, remote_paths))
        source_path, destination_path = remote_paths
        name = source_path.rsplit("/", 1)[-1]
        path = destination_path.split(":", 1)[1].rstrip("/") + "/" + name
        self.destination.items[path] = StorageItem(
            name=name, path=path, is_dir=False, size_bytes=7, provider_type="GOOGLE_DRIVE"
        )
        return b"", b""

    async def copy_local_directory_to_remote(
        self, local_directory: Path, _remote_destination, _config, _job_id, **_kwargs
    ):
        self.directory_trees.append(
            sorted(
                path.relative_to(local_directory).as_posix() for path in local_directory.rglob("*")
            )
        )


class SelectiveFailureCopyRunner(FakeCopyRunner):
    async def run(self, command, remote_paths, config, job_id, **kwargs):
        if remote_paths[0].endswith("failed.bin"):
            raise RcloneCommandError(23, b"temporary failure PRIVATE STDERR MARKER")
        return await super().run(command, remote_paths, config, job_id, **kwargs)


async def run_smb_transfer_case(
    tmp_path: Path,
    files: dict[str, int],
    *,
    conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
    preexisting_destination_items: list[StorageItem] | None = None,
    failing_runner: bool = False,
    published_progress: list[float] | None = None,
):
    store = MemoryStore()
    actor = uuid.uuid4()
    source_connection = ConnectionRecord(
        id=uuid.uuid4(), name="NAS", provider_type="SMB", created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    destination_connection = ConnectionRecord(
        id=uuid.uuid4(), name="Drive", provider_type="GOOGLE_DRIVE", created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    store.connections[source_connection.id] = source_connection
    store.connections[destination_connection.id] = destination_connection
    transfer = TransferRecord(
        id=uuid.uuid4(), public_id="TRF-2026-000098", requested_by=actor,
        source_connection_id=source_connection.id,
        destination_connection_id=destination_connection.id,
        source_paths=["/NAS"], destination_path="/Archive", conflict_policy=conflict_policy,
    )
    store.transfers[transfer.id] = transfer
    source = MultiFileSmbTree(files)
    destination = FakeCloudDestination()
    for item in preexisting_destination_items or []:
        destination.items[item.path] = item
    runner_type = SelectiveFailureCopyRunner if failing_runner else FakeCopyRunner
    runner = runner_type(destination)

    async def publish(published_transfer):
        if published_progress is not None:
            published_progress.append(published_transfer.progress_percent)
        return None

    async def not_cancelled():
        return False

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store,
        publish,
        not_cancelled,
        runner=runner,
    )
    executor._provider = lambda connection: (
        source if connection.provider_type == "SMB" else destination
    )
    await executor._run_smb_transfer(transfer, source_connection, destination_connection)
    return store, transfer


@pytest.mark.asyncio
async def test_smb_inventory_is_persisted_page_by_page_above_legacy_file_limit(tmp_path):
    store = MemoryStore()
    transfer = TransferRecord(
        id=uuid.uuid4(),
        public_id="TRF-2026-000001",
        requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(),
        destination_connection_id=uuid.uuid4(),
        source_paths=["/large"],
        destination_path="/Archive",
    )
    store.transfers[transfer.id] = transfer
    source = LargeSmbListing(10_001)

    async def publish(_transfer):
        return None

    async def not_cancelled():
        return False

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store,
        publish,
        not_cancelled,
        runner=object(),
    )

    await executor._build_smb_manifest(transfer, source)

    assert source.pages == list(range(0, 10_001, 200))
    assert transfer.files_total == 10_001
    assert transfer.bytes_total == 10_001 * 1024
    assert (await store.aget_transfer_manifest_counts(transfer.id)) == {
        "total": 10_002,
        "pending": 10_001,
        "copied": 0,
        "skipped": 0,
        "failed": 0,
    }
    first_page = await store.alist_transfer_manifest_items(
        transfer.id, statuses={"PENDING"}, after_id=None, limit=200
    )
    assert len(first_page) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_size", [5, 0])
async def test_smb_failed_manifest_item_does_not_report_100_percent(tmp_path, failed_size):
    store, transfer = await run_smb_transfer_case(
        tmp_path,
        {"copied.bin": 7, "failed.bin": failed_size},
        failing_runner=True,
    )

    assert transfer.status == "FAILED"
    assert transfer.bytes_transferred == 7
    if failed_size:
        assert transfer.bytes_transferred < transfer.bytes_total
    assert transfer.progress_percent < 100
    assert transfer.files_completed == transfer.files_total == 2
    counts = await store.aget_transfer_manifest_counts(transfer.id)
    assert counts["copied"] == 1
    assert counts["failed"] == 1


@pytest.mark.asyncio
async def test_smb_live_progress_never_reaches_100_for_a_failed_empty_file(tmp_path):
    published_progress: list[float] = []
    await run_smb_transfer_case(
        tmp_path,
        {"failed.bin": 0},
        failing_runner=True,
        published_progress=published_progress,
    )

    assert published_progress
    assert max(published_progress) < 100


@pytest.mark.asyncio
async def test_smb_skipped_manifest_item_reports_warning_summary(tmp_path):
    skipped_item = StorageItem(
        name="skipped.bin",
        path="/Archive/NAS/skipped.bin",
        is_dir=False,
        size_bytes=5,
        provider_type="GOOGLE_DRIVE",
    )
    store, transfer = await run_smb_transfer_case(
        tmp_path,
        {"copied.bin": 7, "skipped.bin": 5},
        conflict_policy="SKIP",
        preexisting_destination_items=[skipped_item],
    )

    assert transfer.status == "COMPLETED_WITH_WARNINGS"
    assert transfer.bytes_total == 12
    assert transfer.bytes_transferred == 7
    assert transfer.progress_percent < 100
    counts = await store.aget_transfer_manifest_counts(transfer.id)
    assert counts["copied"] == 1
    assert counts["skipped"] == 1


@pytest.mark.asyncio
async def test_smb_copy_failure_records_safe_rclone_diagnostics(tmp_path):
    store, transfer = await run_smb_transfer_case(
        tmp_path,
        {"copied.bin": 7, "failed.bin": 5},
        failing_runner=True,
    )

    failure_events = [
        event
        for event in await store.alist_transfer_events(transfer.id)
        if event["event_type"] == "SMB_FILE_COPY_FAILED"
    ]
    assert len(failure_events) == 1
    details = failure_events[0]["technical_details"]
    assert details["exception_type"] == "RcloneCommandError"
    assert details["return_code"] == 23
    assert details["retryable"] is True
    assert "PRIVATE STDERR MARKER" not in str(details)


@pytest.mark.asyncio
async def test_smb_to_google_drive_copies_file_and_preserves_empty_directories(tmp_path):
    store = MemoryStore()
    actor = uuid.uuid4()
    source_connection = ConnectionRecord(
        id=uuid.uuid4(),
        name="NAS",
        provider_type="SMB",
        created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    destination_connection = ConnectionRecord(
        id=uuid.uuid4(),
        name="Drive",
        provider_type="GOOGLE_DRIVE",
        created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    store.connections[source_connection.id] = source_connection
    store.connections[destination_connection.id] = destination_connection
    transfer = TransferRecord(
        id=uuid.uuid4(),
        public_id="TRF-2026-000002",
        requested_by=actor,
        source_connection_id=source_connection.id,
        destination_connection_id=destination_connection.id,
        source_paths=["/NAS"],
        destination_path="/Archive",
    )
    store.transfers[transfer.id] = transfer
    smb = SmallSmbTree()
    cloud = FakeCloudDestination()
    runner = FakeCopyRunner(cloud)

    async def publish(_transfer):
        return None

    async def not_cancelled():
        return False

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store,
        publish,
        not_cancelled,
        runner=runner,
    )
    executor._provider = lambda connection: smb if connection.provider_type == "SMB" else cloud

    await executor._run_smb_transfer(transfer, source_connection, destination_connection)

    assert runner.commands == [
        ("copy", [smb.remote_path("/NAS/report.pdf"), cloud.remote_path("/Archive/NAS")])
    ]
    assert runner.directory_trees == [["NAS", "NAS/Empty"]]
    assert transfer.status == "COMPLETED"
    assert transfer.files_completed == transfer.files_total == 1
    assert transfer.bytes_transferred == transfer.bytes_total == 7
    assert (await store.aget_transfer_manifest_counts(transfer.id))["copied"] == 1


@pytest.mark.asyncio
async def test_smb_inventory_reports_unavailable_source_distinctly(tmp_path):
    class OfflineSmbTree(SmallSmbTree):
        async def list_items(self, *_args, **_kwargs):
            raise RcloneCommandError(1, b"network is unreachable")

    store = MemoryStore()
    actor = uuid.uuid4()
    source_connection = ConnectionRecord(
        id=uuid.uuid4(), name="NAS", provider_type="SMB", created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    destination_connection = ConnectionRecord(
        id=uuid.uuid4(), name="Drive", provider_type="GOOGLE_DRIVE", created_by=actor,
        encrypted_credentials=b"encrypted",
    )
    store.connections[source_connection.id] = source_connection
    store.connections[destination_connection.id] = destination_connection
    transfer = TransferRecord(
        id=uuid.uuid4(), public_id="TRF-2026-000003", requested_by=actor,
        source_connection_id=source_connection.id,
        destination_connection_id=destination_connection.id,
        source_paths=["/NAS"], destination_path="/Archive",
    )
    store.transfers[transfer.id] = transfer

    async def publish(_transfer):
        return None

    async def not_cancelled():
        return False

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store, publish, not_cancelled, runner=object(),
    )
    executor._provider = lambda connection: (
        OfflineSmbTree() if connection.provider_type == "SMB" else FakeCloudDestination()
    )

    await executor._run_smb_transfer(transfer, source_connection, destination_connection)

    assert transfer.status == "FAILED"
    events = await store.alist_transfer_events(transfer.id)
    assert events[-1]["provider_error_code"] == "SMB_SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_smb_file_rejects_source_with_changed_modification_time(tmp_path):
    source = SmallSmbTree()
    source.items["/NAS/report.pdf"] = source.items["/NAS/report.pdf"].model_copy(
        update={"modified_at": datetime(2026, 2, 1, tzinfo=UTC)}
    )
    manifest_item = type(
        "ManifestItem",
        (),
        {
            "source_path": "/NAS/report.pdf",
            "destination_path": "/Archive/NAS/report.pdf",
            "size_bytes": 7,
            "modified_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )()
    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        MemoryStore(), lambda _transfer: asyncio.sleep(0),
        lambda: asyncio.sleep(0, result=False), runner=object(),
    )

    result = await executor._copy_smb_manifest_file(
        TransferRecord(
            id=uuid.uuid4(), public_id="TRF-2026-000004", requested_by=uuid.uuid4(),
            source_connection_id=uuid.uuid4(), destination_connection_id=uuid.uuid4(),
            source_paths=["/NAS"], destination_path="/Archive",
        ),
        source, FakeCloudDestination(), manifest_item,
        destination_is_mega=False, destination_is_api=False, mega_session=None,
    )

    assert result == ("FAILED", None, "SOURCE_CHANGED_DURING_MIGRATION")


@pytest.mark.asyncio
async def test_smb_inventory_rejects_root_items_that_map_to_same_destination(tmp_path):
    class CollisionSource:
        async def get_item(self, path):
            if path not in {"/DeptA/report.csv", "/DeptB/report.csv"}:
                return None
            return StorageItem(
                name="report.csv", path=path, is_dir=False, size_bytes=1, provider_type="SMB"
            )

    store = MemoryStore()
    transfer = TransferRecord(
        id=uuid.uuid4(), public_id="TRF-2026-000005", requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(), destination_connection_id=uuid.uuid4(),
        source_paths=["/DeptA/report.csv", "/DeptB/report.csv"],
        destination_path="/Archive",
    )
    store.transfers[transfer.id] = transfer

    async def publish(_transfer):
        return None

    async def not_cancelled():
        return False

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store, publish, not_cancelled, runner=object(),
    )

    with pytest.raises(executor._PreflightError) as error:
        await executor._build_smb_manifest(transfer, CollisionSource())
    assert error.value.error_code == "SMB_DESTINATION_PATH_COLLISION"


@pytest.mark.asyncio
async def test_smb_resume_reconciles_file_uploaded_before_worker_restart(tmp_path):
    store = MemoryStore()
    source = SmallSmbTree()
    cloud = FakeCloudDestination()
    cloud.items["/Archive/NAS/report.pdf"] = StorageItem(
        name="report.pdf", path="/Archive/NAS/report.pdf", is_dir=False,
        size_bytes=7, provider_type="GOOGLE_DRIVE",
    )
    runner = FakeCopyRunner(cloud)
    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        store, lambda _transfer: asyncio.sleep(0),
        lambda: asyncio.sleep(0, result=False), runner=runner,
    )
    manifest_item = type(
        "ManifestItem",
        (),
        {
            "source_path": "/NAS/report.pdf",
            "destination_path": "/Archive/NAS/report.pdf",
            "size_bytes": 7,
            "modified_at": None,
            "copy_started": True,
            "destination_existed_before": False,
        },
    )()
    transfer = TransferRecord(
        id=uuid.uuid4(), public_id="TRF-2026-000006", requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(), destination_connection_id=uuid.uuid4(),
        source_paths=["/NAS"], destination_path="/Archive",
    )

    result = await executor._copy_smb_manifest_file(
        transfer, source, cloud, manifest_item,
        destination_is_mega=False, destination_is_api=False, mega_session=None,
    )

    assert result == ("COPIED", "SIZE", None)
    assert runner.commands == []


@pytest.mark.asyncio
async def test_smb_resume_does_not_count_partial_mega_upload_as_skipped(tmp_path):
    class PartialMegaDestination:
        async def get_item(self, _path):
            return StorageItem(
                name="report.pdf", path="/Archive/NAS/report.pdf", is_dir=False,
                size_bytes=3, provider_type="MEGA",
            )

    executor = TransferExecutor(
        Settings(transfer_temp_path=str(tmp_path), transfer_staging_path=str(tmp_path)),
        MemoryStore(), lambda _transfer: asyncio.sleep(0),
        lambda: asyncio.sleep(0, result=False), runner=object(),
    )
    manifest_item = type(
        "ManifestItem",
        (),
        {
            "source_path": "/NAS/report.pdf",
            "destination_path": "/Archive/NAS/report.pdf",
            "size_bytes": 7,
            "modified_at": None,
            "copy_started": True,
            "destination_existed_before": False,
        },
    )()
    transfer = TransferRecord(
        id=uuid.uuid4(), public_id="TRF-2026-000007", requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(), destination_connection_id=uuid.uuid4(),
        source_paths=["/NAS"], destination_path="/Archive",
    )

    result = await executor._copy_smb_manifest_file(
        transfer, SmallSmbTree(), PartialMegaDestination(), manifest_item,
        destination_is_mega=True, destination_is_api=False, mega_session=PartialMegaDestination(),
    )

    assert result == ("FAILED", None, "DESTINATION_VERIFICATION_FAILED")
