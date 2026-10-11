from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.security.credentials import CredentialCipher
from app.services.megacmd import MegaCmdCancelledError, MegaCmdCommandError
from app.services.memory_store import MemoryStore
from app.services.rclone import RcloneCancelledError, RcloneCommandError
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings


class ProgressRunner:
    def __init__(self):
        self.calls = []

    async def run(self, command, paths, config, job_id, **kwargs):
        self.calls.append((command, paths, config, kwargs))
        if command == "lsjson":
            return b'[{"Name":"source.txt","IsDir":false,"Size":4}]', b""
        await kwargs["line_callback"](
            '{"level":"notice","msg":"Transferred: 4 B / 4 B, 100%, 1 KiB/s, ETA 0s"}'
        )
        return b"", b""


def _item(path: str, *, is_dir: bool = False, size: int | None = None, provider: str):
    return StorageItem(
        name=path.rsplit("/", 1)[-1] or "/",
        path=path,
        is_dir=is_dir,
        size_bytes=size,
        provider_type=provider,
    )


class FakeStorage:
    def __init__(self, connection_id: uuid.UUID, provider_type: str, items: list[StorageItem]):
        self.connection_id = connection_id
        self.provider_type = provider_type
        self.items = {item.path: item for item in items}
        self.open_count = 0
        self.uploads: list[tuple[Path, str]] = []
        self.failure: str | None = None
        self.download_attempts = 0
        self.upload_attempts = 0
        self.created_directories: list[str] = []

    async def get_item(self, path: str):
        return self.items.get(path)

    async def list_items(self, path: str, cursor=None, page_size=200, **kwargs):
        parent = path.rstrip("/") or "/"
        prefix = "/" if parent == "/" else f"{parent}/"
        children = [
            item
            for item in self.items.values()
            if item.path.startswith(prefix)
            and item.path[len(prefix) :]
            and "/" not in item.path[len(prefix) :]
        ]
        offset = int(cursor or 0)
        page = children[offset : offset + page_size]
        next_offset = offset + page_size
        return PaginatedItems(
            items=page,
            next_cursor=str(next_offset) if next_offset < len(children) else None,
            total_count=len(children),
        )

    def config_content(self):
        return f"[ctm_{self.connection_id.hex}]\ntype = drive\n"

    def remote_path(self, path: str):
        return f"ctm_{self.connection_id.hex}:{path}"

    @asynccontextmanager
    async def open_session(self, **kwargs) -> AsyncIterator[FakeMegaSession]:
        self.open_count += 1
        yield FakeMegaSession(self)


class FakeMegaSession:
    def __init__(self, provider: FakeStorage):
        self.provider = provider

    async def get_item(self, path: str):
        return await self.provider.get_item(path)

    async def mkdir(self, path: str):
        self.provider.created_directories.append(path)

    async def list_items(self, path: str, cursor=None, page_size=200, **kwargs):
        return await self.provider.list_items(path, cursor, page_size, **kwargs)

    async def download_file(self, remote_path: str, destination_dir: Path, **kwargs):
        self.provider.download_attempts += 1
        if (
            self.provider.failure == "mega_download_transient_once"
            and self.provider.download_attempts == 1
        ):
            raise MegaCmdCommandError(7, b"temporary failure")
        if self.provider.failure and self.provider.failure.startswith("mega_download") and (
            self.provider.failure != "mega_download_transient_once"
        ):
            if self.provider.failure.endswith("cancel"):
                raise MegaCmdCancelledError("cancelled")
            raise RuntimeError("download failed")
        local_file = destination_dir / remote_path.rsplit("/", 1)[-1]
        local_file.write_bytes(b"x" * (self.provider.items[remote_path].size_bytes or 0))
        if kwargs.get("line_callback"):
            await kwargs["line_callback"]("TRANSFERRING ||####||(1/1 B: 100.00 %)")
        return local_file

    async def upload_file(self, local_file: Path, remote_folder: str, **kwargs):
        self.provider.upload_attempts += 1
        if (
            self.provider.failure == "mega_upload_partial_transient"
            and self.provider.upload_attempts == 1
        ):
            remote_path = f"{remote_folder.rstrip('/')}/{local_file.name}"
            self.provider.items[remote_path] = _item(
                remote_path, size=0, provider="MEGA"
            )
            raise MegaCmdCommandError(7, b"temporary failure")
        if (
            self.provider.failure == "mega_upload_transient_once"
            and self.provider.upload_attempts == 1
        ):
            raise MegaCmdCommandError(7, b"temporary failure")
        if self.provider.failure == "mega_upload":
            raise RuntimeError("upload failed")
        if self.provider.failure == "mega_upload_cancel":
            raise MegaCmdCancelledError("cancelled")
        remote_path = f"{remote_folder.rstrip('/')}/{local_file.name}"
        self.provider.uploads.append((local_file, remote_folder))
        self.provider.items[remote_path] = _item(
            remote_path,
            size=local_file.stat().st_size,
            provider="MEGA",
        )
        if kwargs.get("line_callback"):
            await kwargs["line_callback"]("TRANSFERRING ||####||(1/1 B: 100.00 %)")


class LocalCopyRunner:
    def __init__(self, staging_root: Path):
        self.staging_root = staging_root
        self.uploaded: list[tuple[Path, str]] = []
        self.downloaded: list[tuple[str, Path]] = []
        self.created_directories: list[tuple[str, list[str]]] = []
        self.active_areas: list[list[Path]] = []
        self.failure: str | None = None
        self.upload_attempts = 0

    async def copy_local_to_remote(self, local_file, remote_destination, config, job_id, **kwargs):
        self.upload_attempts += 1
        self.active_areas.append(list(self.staging_root.iterdir()))
        if self.failure == "rclone_upload_transient_once" and self.upload_attempts == 1:
            raise RcloneCommandError(1, b"temporary failure")
        if self.failure == "rclone_upload":
            raise RcloneCommandError(1, b"temporary failure")
        if self.failure == "rclone_upload_cancel":
            raise RcloneCancelledError("cancelled")
        self.uploaded.append((local_file, remote_destination))
        if kwargs.get("line_callback"):
            await kwargs["line_callback"](
                '{"level":"notice","msg":"Transferred: 1 B / 1 B, 100%, 1 KiB/s, ETA 0s"}'
            )

    async def copy_local_directory_to_remote(
        self, local_directory, remote_destination, config, job_id, **kwargs
    ):
        self.active_areas.append(list(self.staging_root.iterdir()))
        directories = sorted(
            path.relative_to(local_directory).as_posix()
            for path in local_directory.rglob("*")
            if path.is_dir()
        )
        self.created_directories.append((remote_destination, directories))

    async def copy_remote_to_local(self, remote_source, local_directory, config, job_id, **kwargs):
        self.active_areas.append(list(self.staging_root.iterdir()))
        if self.failure == "rclone_download":
            raise RcloneCommandError(1, b"temporary failure")
        if self.failure == "rclone_download_cancel":
            raise RcloneCancelledError("cancelled")
        self.downloaded.append((remote_source, local_directory))
        basename = remote_source.rsplit("/", 1)[-1]
        (local_directory / basename).write_bytes(b"x")
        if kwargs.get("line_callback"):
            await kwargs["line_callback"](
                '{"level":"notice","msg":"Transferred: 1 B / 1 B, 100%, 1 KiB/s, ETA 0s"}'
            )


def _cross_provider_executor(
    tmp_path: Path,
    *,
    source_type: str,
    destination_type: str,
    source_items: list[StorageItem],
    destination_items: list[StorageItem] | None = None,
    source_paths: list[str] | None = None,
    runner: LocalCopyRunner | None = None,
):
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source_id = uuid.uuid4()
    destination_id = uuid.uuid4()
    source = store.create_connection("Source", source_type, requester.id, source_id)
    destination = store.create_connection(
        "Destination", destination_type, requester.id, destination_id
    )
    transfer = store.create_transfer(
        requester.id,
        source.id,
        destination.id,
        source_paths or ["/source"],
        "/backup",
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        transfer_temp_path=str(tmp_path / "rclone-config"),
        transfer_staging_path=str(tmp_path / "staging"),
    )
    Path(settings.transfer_staging_path).mkdir(parents=True)
    providers = {
        source_id: FakeStorage(source_id, source_type, source_items),
        destination_id: FakeStorage(destination_id, destination_type, destination_items or []),
    }
    snapshots = []

    async def publish(record):
        snapshots.append(record.public_dict())
        await store.asave_transfer(record)

    async def never_cancel():
        return False

    copy_runner = runner or LocalCopyRunner(Path(settings.transfer_staging_path))
    executor = TransferExecutor(settings, store, publish, never_cancel, runner=copy_runner)
    executor._provider = lambda connection: providers[connection.id]
    return executor, transfer, providers, copy_runner, snapshots, store


def _nested_tree(provider_type: str, sizes: tuple[int, int] = (2, 3)) -> list[StorageItem]:
    return [
        _item("/source", is_dir=True, provider=provider_type),
        _item("/source/a.txt", size=sizes[0], provider=provider_type),
        _item("/source/sub", is_dir=True, provider=provider_type),
        _item("/source/sub/b.txt", size=sizes[1], provider=provider_type),
    ]


@pytest.mark.asyncio
async def test_mega_to_drive_preserves_nested_paths_deduplicates_and_cleans_each_file(tmp_path):
    executor, transfer, providers, runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="MEGA",
        destination_type="GOOGLE_DRIVE",
        source_items=_nested_tree("MEGA")
        + [_item("/source/empty", is_dir=True, provider="MEGA")],
        source_paths=["/source", "/source/sub/b.txt", "//source//"],
    )

    await executor.execute(transfer.id)

    mega_source = providers[transfer.source_connection_id]
    drive_destination = providers[transfer.destination_connection_id]
    assert mega_source.open_count == 1
    assert [remote for _local, remote in runner.uploaded] == [
        f"ctm_{drive_destination.connection_id.hex}:/backup/source",
        f"ctm_{drive_destination.connection_id.hex}:/backup/source/sub",
    ]
    assert runner.created_directories == [
        (
            f"ctm_{drive_destination.connection_id.hex}:/backup",
            ["source", "source/empty", "source/sub"],
        )
    ]
    assert transfer.files_total == transfer.files_completed == 2
    assert transfer.bytes_total == transfer.bytes_transferred == 5
    assert transfer.status == "COMPLETED"
    assert len(runner.active_areas) == 3
    assert all(len(areas) == 1 for areas in runner.active_areas)
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []
    live_progress = [
        snapshot
        for snapshot in _snapshots
        if snapshot["status"] == "RUNNING" and snapshot["current_speed_bps"] is not None
    ]
    assert live_progress
    assert all(snapshot["current_item"].startswith("/source/") for snapshot in live_progress)
    assert str(executor.settings.transfer_staging_path) not in str(_snapshots)


@pytest.mark.asyncio
async def test_mega_to_drive_preserves_an_empty_selected_folder(tmp_path):
    executor, transfer, providers, runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="MEGA",
        destination_type="GOOGLE_DRIVE",
        source_items=[
            _item("/empty-folder", is_dir=True, provider="MEGA"),
            _item("/empty-folder/nested", is_dir=True, provider="MEGA"),
        ],
        source_paths=["/empty-folder"],
    )

    await executor.execute(transfer.id)

    drive_destination = providers[transfer.destination_connection_id]
    assert runner.created_directories == [
        (
            f"ctm_{drive_destination.connection_id.hex}:/backup",
            ["empty-folder", "empty-folder/nested"],
        )
    ]
    assert transfer.files_total == transfer.files_completed == 0
    assert transfer.status == "COMPLETED"
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []


@pytest.mark.asyncio
async def test_drive_to_mega_preserves_nested_paths_and_skips_existing_destination(tmp_path):
    existing = _item("/backup/sub/b.txt", size=3, provider="MEGA")
    executor, transfer, providers, runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="GOOGLE_DRIVE",
        destination_type="MEGA",
        source_items=_nested_tree("GOOGLE_DRIVE")
        + [_item("/source/sub/c.txt", size=1, provider="GOOGLE_DRIVE")],
        destination_items=[existing],
        source_paths=["/source"],
    )

    await executor.execute(transfer.id)

    mega_destination = providers[transfer.destination_connection_id]
    assert mega_destination.open_count == 1
    assert mega_destination.created_directories == ["/backup/sub"]
    assert [folder for _local, folder in mega_destination.uploads] == ["/backup", "/backup/sub"]
    assert [remote for remote, _local in runner.downloaded] == [
        f"ctm_{providers[transfer.source_connection_id].connection_id.hex}:/source/a.txt",
        f"ctm_{providers[transfer.source_connection_id].connection_id.hex}:/source/sub/c.txt",
    ]
    assert transfer.warning_count == 1
    assert transfer.files_completed == transfer.files_total == 3
    assert transfer.bytes_transferred == 3
    assert transfer.status == "COMPLETED_WITH_WARNINGS"
    assert len(runner.active_areas) == 2
    assert all(len(areas) == 1 for areas in runner.active_areas)
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []


@pytest.mark.asyncio
async def test_seafile_rclone_to_mega_creates_nested_destination_directories(tmp_path):
    executor, transfer, providers, _runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="SEAFILE",
        destination_type="MEGA",
        source_items=_nested_tree("SEAFILE"),
        source_paths=["/source"],
    )

    await executor.execute(transfer.id)

    mega_destination = providers[transfer.destination_connection_id]
    assert mega_destination.created_directories == ["/backup/sub"]
    assert [folder for _local, folder in mega_destination.uploads] == ["/backup", "/backup/sub"]
    assert transfer.status == "COMPLETED"


@pytest.mark.parametrize(
    ("source_type", "destination_type", "failure", "expected_status"),
    [
        ("MEGA", "GOOGLE_DRIVE", "mega_download", "FAILED"),
        ("MEGA", "GOOGLE_DRIVE", "rclone_upload", "FAILED"),
        ("GOOGLE_DRIVE", "MEGA", "rclone_download", "FAILED"),
        ("GOOGLE_DRIVE", "MEGA", "mega_upload", "FAILED"),
        ("MEGA", "GOOGLE_DRIVE", "mega_download_cancel", "CANCELLED"),
        ("MEGA", "GOOGLE_DRIVE", "rclone_upload_cancel", "CANCELLED"),
        ("GOOGLE_DRIVE", "MEGA", "rclone_download_cancel", "CANCELLED"),
        ("GOOGLE_DRIVE", "MEGA", "mega_upload_cancel", "CANCELLED"),
    ],
)
@pytest.mark.asyncio
async def test_cross_provider_failures_and_cancellation_always_clean_staging(
    tmp_path, source_type, destination_type, failure, expected_status
):
    runner = LocalCopyRunner(tmp_path / "staging")
    executor, transfer, providers, _runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type=source_type,
        destination_type=destination_type,
        source_items=[_item("/file.txt", size=1, provider=source_type)],
        source_paths=["/file.txt"],
        runner=runner,
    )
    executor.settings.transfer_max_retries = 0
    if failure.startswith("mega_"):
        mega_provider = providers[
            transfer.source_connection_id
            if source_type == "MEGA"
            else transfer.destination_connection_id
        ]
        mega_provider.failure = failure
    else:
        runner.failure = failure

    await executor.execute(transfer.id)

    assert transfer.status == expected_status
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []


@pytest.mark.asyncio
async def test_mega_to_drive_retries_transient_copy_with_fresh_staging(tmp_path):
    runner = LocalCopyRunner(tmp_path / "staging")
    executor, transfer, providers, _runner, snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="MEGA",
        destination_type="GOOGLE_DRIVE",
        source_items=[_item("/file.txt", size=1, provider="MEGA")],
        source_paths=["/file.txt"],
        runner=runner,
    )
    executor.settings.transfer_max_retries = 1
    runner.failure = "rclone_upload_transient_once"

    async def no_delay(_seconds):
        return None

    executor.sleep = no_delay
    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert runner.upload_attempts == 2
    assert transfer.attempt_count == 2
    assert transfer.error_count == 1
    assert any(snapshot["status"] == "RETRYING" for snapshot in snapshots)
    assert len(runner.active_areas) == 2
    assert all(len(areas) == 1 for areas in runner.active_areas)
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []
    assert providers[transfer.source_connection_id].download_attempts == 2


@pytest.mark.asyncio
async def test_mega_download_retries_classified_transient_command_error(tmp_path):
    executor, transfer, providers, _runner, _snapshots, _store = _cross_provider_executor(
        tmp_path,
        source_type="MEGA",
        destination_type="GOOGLE_DRIVE",
        source_items=[_item("/file.txt", size=1, provider="MEGA")],
        source_paths=["/file.txt"],
    )
    executor.settings.transfer_max_retries = 1
    providers[transfer.source_connection_id].failure = "mega_download_transient_once"

    async def no_delay(_seconds):
        return None

    executor.sleep = no_delay
    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert providers[transfer.source_connection_id].download_attempts == 2
    assert transfer.attempt_count == 2


@pytest.mark.parametrize(
    ("failure", "destination_visible"),
    [
        ("mega_upload_partial_transient", True),
        ("mega_upload_transient_once", False),
    ],
)
@pytest.mark.asyncio
async def test_drive_to_mega_does_not_retry_after_ambiguous_remote_upload(
    tmp_path, failure, destination_visible
):
    executor, transfer, providers, runner, _snapshots, store = _cross_provider_executor(
        tmp_path,
        source_type="GOOGLE_DRIVE",
        destination_type="MEGA",
        source_items=[_item("/file.txt", size=1, provider="GOOGLE_DRIVE")],
        source_paths=["/file.txt"],
    )
    executor.settings.transfer_max_retries = 2
    mega_destination = providers[transfer.destination_connection_id]
    mega_destination.failure = failure
    runner.failure = None

    async def no_delay(_seconds):
        return None

    executor.sleep = no_delay
    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert mega_destination.upload_attempts == 1
    assert transfer.attempt_count == 1
    assert ("/backup/file.txt" in mega_destination.items) is destination_visible
    assert list(Path(executor.settings.transfer_staging_path).iterdir()) == []
    events = await store.alist_transfer_events(transfer.id)
    assert events[-1]["provider_error_code"] == "MEGA_DESTINATION_STATE_UNCERTAIN"


@pytest.mark.asyncio
async def test_real_transfer_uses_combined_ephemeral_provider_config_and_copy_only(tmp_path):
    key_file = tmp_path / "master-key"
    key_file.write_bytes(b"m" * 32)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        master_encryption_key_file=str(key_file),
        transfer_temp_path=str(tmp_path / "rclone"),
    )
    cipher = CredentialCipher(b"m" * 32)
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source_id = uuid.uuid4()
    destination_id = uuid.uuid4()
    source_creds = {
        "client_id": "source-client",
        "client_secret": "source-secret",
        "refresh_token": "source-refresh-token",
    }
    destination_creds = {
        "client_id": "client",
        "client_secret": "drive-secret",
        "refresh_token": "refresh-token",
    }
    source = store.create_connection(
        "Drive origen",
        "GOOGLE_DRIVE",
        requester.id,
        source_id,
        cipher.encrypt(source_id, source_creds),
    )
    destination = store.create_connection(
        "Drive",
        "GOOGLE_DRIVE",
        requester.id,
        destination_id,
        cipher.encrypt(destination_id, destination_creds),
    )
    transfer = store.create_transfer(
        requester.id, source.id, destination.id, ["/source.txt"], "/backup", "SKIP"
    )
    runner = ProgressRunner()
    snapshots = []

    async def publish(record):
        snapshots.append(record.public_dict())
        await store.asave_transfer(record)

    async def never_cancel():
        return False

    executor = TransferExecutor(settings, store, publish, never_cancel, runner=runner)
    await executor.execute(transfer.id)

    copy_calls = [call for call in runner.calls if call[0] == "copy"]
    assert len(copy_calls) == 1
    command, paths, config, options = copy_calls[0]
    assert command == "copy"
    assert paths == [
        "ctm_" + source_id.hex + ":/source.txt",
        "ctm_" + destination_id.hex + ":/backup",
    ]
    assert f"[ctm_{source_id.hex}]" in config
    assert f"[ctm_{destination_id.hex}]" in config
    assert options["conflict_policy"] == "SKIP"
    assert transfer.status == "COMPLETED"
    assert transfer.bytes_transferred == 4
    assert transfer.progress_percent == 100
    assert snapshots[-1]["status"] == "COMPLETED"
    progress_snapshot = next(item for item in snapshots if item["current_speed_bps"] is not None)
    assert progress_snapshot["current_speed_bps"] == 1024
    assert progress_snapshot["average_speed_bps"] is not None
    assert "refresh-token" not in str(snapshots)
    assert "mega-password" not in str(snapshots)


@pytest.mark.asyncio
async def test_multi_path_progress_aggregates_each_path_without_regressing(tmp_path):
    key_file = tmp_path / "master-key"
    key_file.write_bytes(b"m" * 32)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        master_encryption_key_file=str(key_file),
        transfer_temp_path=str(tmp_path / "rclone"),
    )
    cipher = CredentialCipher(b"m" * 32)
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source_id = uuid.uuid4()
    destination_id = uuid.uuid4()
    source = store.create_connection(
        "Source",
        "GOOGLE_DRIVE",
        requester.id,
        source_id,
        cipher.encrypt(
            source_id,
            {"client_id": "id", "client_secret": "secret", "refresh_token": "token"},
        ),
    )
    destination = store.create_connection(
        "Destination", "GOOGLE_DRIVE", requester.id, destination_id,
        cipher.encrypt(
            destination_id,
            {"client_id": "client", "client_secret": "secret", "refresh_token": "token"},
        ),
    )
    transfer = store.create_transfer(
        requester.id, source.id, destination.id, ["/one.txt", "/two.txt"], "/backup", "SKIP"
    )
    snapshots = []

    class MultiPathRunner:
        async def run(self, command, paths, config, job_id, **kwargs):
            if command == "lsjson":
                return (
                    b'[{"Name":"one.txt","IsDir":false,"Size":10},'
                    b'{"Name":"two.txt","IsDir":false,"Size":1000}]',
                    b"",
                )
            local_progress = 100 if paths[0].endswith("/one.txt") else 10
            bytes_transferred = 10 if local_progress == 100 else 100
            bytes_total = 10 if local_progress == 100 else 1000
            eta = 0 if local_progress == 100 else 90
            await kwargs["line_callback"](
                '{"level":"notice","msg":"Transferred: '
                f'{bytes_transferred} B / {bytes_total} B, {local_progress}%, '
                f'10 B/s, ETA {eta}s\\nTransferred: 1 / 1, 100%"'
                '}'
            )
            return b"", b""

    async def publish(record):
        snapshots.append(record.public_dict())
        await store.asave_transfer(record)

    async def never_cancel():
        return False

    await TransferExecutor(
        settings, store, publish, never_cancel, runner=MultiPathRunner()
    ).execute(transfer.id)

    progress = [
        snapshot["progress_percent"]
        for snapshot in snapshots
        if snapshot["status"] == "RUNNING" and snapshot["current_speed_bps"] is not None
    ]
    assert progress == [0.99, 10.89]
    assert [
        snapshot["bytes_total"]
        for snapshot in snapshots
        if snapshot["status"] == "RUNNING" and snapshot["current_speed_bps"] is not None
    ] == [1010, 1010]
    assert [
        snapshot["files_total"]
        for snapshot in snapshots
        if snapshot["status"] == "RUNNING" and snapshot["current_speed_bps"] is not None
    ] == [2, 2]
    second_path_snapshot = next(
        snapshot
        for snapshot in snapshots
        if snapshot["status"] == "RUNNING" and snapshot["current_speed_bps"] is not None
        and snapshot["bytes_transferred"] == 110
    )
    assert second_path_snapshot["estimated_remaining_seconds"] == 90
    assert transfer.progress_percent == 100.0


@pytest.mark.asyncio
async def test_retry_backoff_is_interruptible_by_cancellation(tmp_path):
    key_file = tmp_path / "master-key"
    key_file.write_bytes(b"m" * 32)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        master_encryption_key_file=str(key_file),
        transfer_temp_path=str(tmp_path / "rclone"),
        transfer_max_retries=3,
    )
    cipher = CredentialCipher(b"m" * 32)
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source_id = uuid.uuid4()
    destination_id = uuid.uuid4()
    source = store.create_connection(
        "Drive origen",
        "GOOGLE_DRIVE",
        requester.id,
        source_id,
        cipher.encrypt(
            source_id,
            {"client_id": "id", "client_secret": "secret", "refresh_token": "token"},
        ),
    )
    destination = store.create_connection(
        "Drive",
        "GOOGLE_DRIVE",
        requester.id,
        destination_id,
        cipher.encrypt(
            destination_id,
            {"client_id": "client", "client_secret": "secret", "refresh_token": "token"},
        ),
    )
    transfer = store.create_transfer(
        requester.id, source.id, destination.id, ["/source.txt"], "/backup", "SKIP"
    )

    class RetryRunner:
        def __init__(self):
            self.copy_calls = 0

        async def run(self, command, paths, config, job_id, **kwargs):
            if command == "lsjson":
                return b'[{"Name":"source.txt","IsDir":false,"Size":4}]', b""
            self.copy_calls += 1
            raise RcloneCommandError(1, b"temporary failure")

    runner = RetryRunner()
    cancelled = False
    sleep_intervals = []

    async def publish(record):
        await store.asave_transfer(record)

    async def should_cancel():
        return cancelled

    async def controlled_sleep(delay):
        nonlocal cancelled
        sleep_intervals.append(delay)
        assert delay <= 0.25
        cancelled = True

    executor = TransferExecutor(
        settings,
        store,
        publish,
        should_cancel,
        runner=runner,
        sleep=controlled_sleep,
    )
    await executor.execute(transfer.id)

    assert transfer.status == "CANCELLED"
    assert runner.copy_calls == 1
    assert sleep_intervals == [0.25]


@pytest.mark.asyncio
async def test_transfer_failure_records_sanitized_correlated_diagnostics(tmp_path):
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source = store.create_connection("Source", "MEGA", requester.id)
    destination = store.create_connection("Destination", "GOOGLE_DRIVE", requester.id)
    transfer = store.create_transfer(
        requester.id, source.id, destination.id, ["/source.txt"], "/backup"
    )

    async def publish(record):
        await store.asave_transfer(record)

    async def never_cancel():
        return False

    executor = TransferExecutor(Settings(), store, publish, never_cancel)
    await executor._fail(
        transfer,
        error_code="RCLONE_COMMAND_FAILED",
        message="No se pudo completar la copia.",
        technical_details={"return_code": 7, "retryable": False},
    )

    events = await store.alist_transfer_events(transfer.id)
    failure = events[-1]
    assert transfer.status == "FAILED"
    assert failure["provider_error_code"] == "RCLONE_COMMAND_FAILED"
    assert failure["technical_details"] == {"return_code": 7, "retryable": False}
    assert failure["correlation_id"] == str(transfer.id)
