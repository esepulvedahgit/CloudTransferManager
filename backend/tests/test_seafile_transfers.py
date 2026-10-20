from __future__ import annotations

import hashlib
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.providers.seafile_api import SeafileApiCancelledError, SeafileApiError
from app.services.megacmd import MegaCmdCommandError
from app.services.memory_store import MemoryStore
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings


def _item(path: str, *, is_dir: bool = False, data: bytes = b"", provider: str) -> StorageItem:
    return StorageItem(
        name=path.rsplit("/", 1)[-1] or "/",
        path=path,
        is_dir=is_dir,
        size_bytes=None if is_dir else len(data),
        provider_type=provider,
    )


class FakeStorage:
    def __init__(self, connection_id: uuid.UUID, provider_type: str, tree: dict[str, bytes | None]):
        self.connection_id = connection_id
        self.provider_type = provider_type
        self.tree = tree
        self.directories = {path for path, content in tree.items() if content is None}
        self.data = {path: content for path, content in tree.items() if content is not None}
        self.items = {
            path: _item(path, is_dir=content is None, data=content or b"", provider=provider_type)
            for path, content in tree.items()
        }
        self.upload_attempts = 0
        self.fail_upload_once = False
        self.corrupt_readback = False
        self.fail_auth = False
        self.cancel_after_download = False
        self.downloaded: list[str] = []
        self.open_count = 0
        self.upload_calls: list[tuple[str, bool]] = []
        self.race_on_upload = False
        self.fail_mega_upload_after_write = False
        self.mega_upload_attempts = 0
        self.normalize_names_on_upload = False

    async def get_item(self, path: str):
        return self.items.get(path)

    @asynccontextmanager
    async def open_session(self, **_kwargs):
        self.open_count += 1
        yield FakeMegaSession(self)

    async def list_items(self, path: str, cursor=None, page_size=200, **_kwargs):
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
        selected = children[offset : offset + page_size]
        next_offset = offset + page_size
        return PaginatedItems(
            items=selected,
            next_cursor=str(next_offset) if next_offset < len(children) else None,
            total_count=len(children),
        )

    def config_content(self):
        return f"[ctm_{self.connection_id.hex}]\ntype = drive\n"

    def remote_path(self, path: str):
        return f"ctm_{self.connection_id.hex}:{path}"

    async def mkdir(self, path: str, **_kwargs):
        self.directories.add(path)
        self.items[path] = _item(path, is_dir=True, provider=self.provider_type)

    async def download_file(self, remote_path: str, target: Path, **kwargs):
        if self.fail_auth:
            raise SeafileApiError(401)
        data = self.data[remote_path]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.downloaded.append(remote_path)
        callback = kwargs.get("should_cancel")
        if self.cancel_after_download and callback and await callback():
            raise SeafileApiCancelledError("cancelled")
        return target

    async def upload_file(self, local_file: Path, remote_path: str, **kwargs):
        self.upload_attempts += 1
        replace = kwargs.get("replace", True)
        self.upload_calls.append((remote_path, replace))
        if self.fail_upload_once and self.upload_attempts == 1:
            partial = local_file.read_bytes()[:3]
            self.data[remote_path] = partial
            self.items[remote_path] = _item(remote_path, data=partial, provider=self.provider_type)
            raise SeafileApiError(503)
        actual_path = remote_path
        if self.normalize_names_on_upload:
            actual_path = unicodedata.normalize("NFC", actual_path)
        if self.race_on_upload and not replace:
            self.race_on_upload = False
            self.data[remote_path] = b"concurrent writer"
            self.items[remote_path] = _item(
                remote_path, data=b"concurrent writer", provider=self.provider_type
            )
            parent, separator, name = remote_path.rpartition("/")
            stem, dot, suffix = name.rpartition(".")
            unique_name = f"{stem} (1).{suffix}" if dot else f"{name} (1)"
            actual_path = f"{parent}{separator}{unique_name}"
        data = local_file.read_bytes()
        self.data[actual_path] = data
        self.items[actual_path] = _item(actual_path, data=data, provider=self.provider_type)
        return actual_path

    async def sha256(self, remote_path: str, **_kwargs):
        data = self.data[remote_path]
        if self.corrupt_readback:
            data += b"corrupt"
        return hashlib.sha256(data).hexdigest()


class FakeMegaSession:
    def __init__(self, provider: FakeStorage):
        self.provider = provider

    async def get_item(self, path: str):
        return await self.provider.get_item(path)

    async def list_items(self, path: str, cursor=None, page_size=200, **kwargs):
        return await self.provider.list_items(path, cursor, page_size, **kwargs)

    async def mkdir(self, path: str):
        await self.provider.mkdir(path)

    async def download_file(self, remote_path: str, destination_dir: Path, **_kwargs):
        target = destination_dir / remote_path.rsplit("/", 1)[-1]
        target.write_bytes(self.provider.data[remote_path])
        return target

    async def upload_file(self, local_file: Path, remote_folder: str, **_kwargs):
        self.provider.mega_upload_attempts += 1
        remote_path = f"{remote_folder.rstrip('/')}/{local_file.name}"
        data = local_file.read_bytes()
        self.provider.data[remote_path] = data
        self.provider.items[remote_path] = _item(
            remote_path, data=data, provider=self.provider.provider_type
        )
        if self.provider.fail_mega_upload_after_write:
            raise MegaCmdCommandError(1, b"connection reset")


class FakeRcloneRunner:
    def __init__(self, providers: dict[uuid.UUID, FakeStorage]):
        self.providers = providers
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.directory_copies: list[tuple[str, list[str]]] = []

    def _provider_path(self, remote: str):
        remote_name, path = remote.split(":", 1)
        connection_id = uuid.UUID(hex=remote_name.removeprefix("ctm_"))
        return self.providers[connection_id], path

    async def copy_remote_to_local(self, remote_source, local_directory, _config, _job, **_kwargs):
        provider, path = self._provider_path(remote_source)
        self.downloads.append(remote_source)
        (local_directory / path.rsplit("/", 1)[-1]).write_bytes(provider.data[path])

    async def copy_local_to_remote(self, local_file, remote_folder, _config, _job, **_kwargs):
        self.uploads.append((remote_folder, local_file.read_bytes()))
        provider, path = self._provider_path(remote_folder)
        remote_path = f"{path.rstrip('/')}/{local_file.name}"
        data = local_file.read_bytes()
        provider.data[remote_path] = data
        provider.items[remote_path] = _item(remote_path, data=data, provider=provider.provider_type)

    async def copy_local_directory_to_remote(
        self, local_directory, remote_destination, _config, _job, **_kwargs
    ):
        directories = sorted(
            path.relative_to(local_directory).as_posix()
            for path in local_directory.rglob("*")
            if path.is_dir()
        )
        self.directory_copies.append((remote_destination, directories))


def _setup(
    tmp_path: Path,
    *,
    source_type: str = "SEAFILE",
    destination_type: str = "SEAFILE",
    source_adapter: str = "SEAFILE_API",
    destination_adapter: str = "SEAFILE_API",
    source_tree: dict[str, bytes | None] | None = None,
    destination_tree: dict[str, bytes | None] | None = None,
    source_paths: list[str] | None = None,
    destination_path: str = "/backup",
    conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
):
    store = MemoryStore()
    requester = store.add_user("operator", "operator@example.test", "hash")
    source_id, destination_id = uuid.uuid4(), uuid.uuid4()
    store.create_connection(
        "Source",
        source_type,
        requester.id,
        source_id,
        encrypted_credentials=b"encrypted-source",
        adapter_type=source_adapter,
    )
    store.create_connection(
        "Destination",
        destination_type,
        requester.id,
        destination_id,
        encrypted_credentials=b"encrypted-destination",
        adapter_type=destination_adapter,
    )
    transfer = store.create_transfer(
        requester.id,
        source_id,
        destination_id,
        source_paths or ["/source"],
        destination_path,
        conflict_policy,
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.sqlite'}",
        transfer_temp_path=str(tmp_path / "rclone-config"),
        transfer_staging_path=str(tmp_path / "staging"),
        transfer_max_retries=1,
    )
    staging = Path(settings.transfer_staging_path)
    staging.mkdir(parents=True)
    providers = {
        source_id: FakeStorage(source_id, source_type, source_tree or {"/source": b"payload"}),
        destination_id: FakeStorage(destination_id, destination_type, destination_tree or {}),
    }
    snapshots = []

    async def publish(record):
        snapshots.append(record.public_dict())
        await store.asave_transfer(record)

    cancel_state = {"cancel": False}

    async def should_cancel():
        return cancel_state["cancel"]

    runner = FakeRcloneRunner(providers)
    executor = TransferExecutor(
        settings,
        store,
        publish,
        should_cancel,
        runner=runner,
        sleep=lambda _seconds: _no_sleep(),
    )
    executor._provider = lambda connection: providers[connection.id]
    return executor, transfer, providers, runner, snapshots, staging, cancel_state, store


async def _no_sleep():
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "destination_type", "source_adapter", "destination_adapter"),
    [
        ("SEAFILE", "GOOGLE_DRIVE", "SEAFILE_API", "RCLONE"),
        ("GOOGLE_DRIVE", "SEAFILE", "RCLONE", "SEAFILE_API"),
        ("SEAFILE", "SEAFILE", "SEAFILE_API", "SEAFILE_API"),
    ],
)
async def test_api_involved_transfer_stages_file_and_completes_after_integrity_check(
    tmp_path, source_type, destination_type, source_adapter, destination_adapter
):
    executor, transfer, providers, runner, _snapshots, staging, _cancel, store = _setup(
        tmp_path,
        source_type=source_type,
        destination_type=destination_type,
        source_adapter=source_adapter,
        destination_adapter=destination_adapter,
    )

    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED", [
        (event.get("provider_error_code"), event.get("technical_details"))
        for event in store.transfer_events.get(transfer.id, [])
    ]
    assert transfer.files_completed == transfer.files_total == 1
    if destination_adapter == "SEAFILE_API":
        destination = providers[transfer.destination_connection_id]
        assert destination.data["/backup/source"] == b"payload"
    else:
        destination_remote = (
            "ctm_" + providers[transfer.destination_connection_id].connection_id.hex + ":/backup"
        )
        assert runner.uploads == [(destination_remote, b"payload")]
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_destination_checksum_mismatch_fails_before_file_progress(tmp_path):
    executor, transfer, providers, _runner, snapshots, staging, _cancel, store = _setup(tmp_path)
    providers[transfer.destination_connection_id].corrupt_readback = True

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert transfer.files_completed == 0
    assert transfer.progress_percent < 100
    assert store.transfer_events[transfer.id][0]["provider_error_code"] == (
        "SEAFILE_INTEGRITY_MISMATCH"
    )
    assert any(snapshot["files_completed"] == 0 for snapshot in snapshots)
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_retry_repeats_resumable_upload_without_fallback(tmp_path):
    executor, transfer, providers, runner, _snapshots, staging, _cancel, _store = _setup(tmp_path)
    destination = providers[transfer.destination_connection_id]
    destination.fail_upload_once = True

    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert destination.upload_attempts == 2
    assert runner.uploads == []
    assert transfer.error_count == 1
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_staging_route_rejects_unknown_size_before_destination_write(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path
    )
    source = providers[transfer.source_connection_id]
    source.items["/source"].size_bytes = None

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert not providers[transfer.destination_connection_id].data
    assert any(
        event["event_type"] == "FAILURE_DIAGNOSTIC"
        and event["provider_error_code"] == "TRANSFER_FILE_SIZE_UNKNOWN"
        for event in _store.transfer_events[transfer.id]
    )
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_source_download_size_must_match_listing_before_copy(tmp_path):
    executor, transfer, providers, runner, _snapshots, staging, _cancel, store = _setup(
        tmp_path,
        source_type="SEAFILE",
        destination_type="GOOGLE_DRIVE",
        source_adapter="SEAFILE_API",
        destination_adapter="RCLONE",
    )
    providers[transfer.source_connection_id].items["/source"].size_bytes = 2

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert runner.uploads == []
    assert store.transfer_events[transfer.id][0]["provider_error_code"] == "SOURCE_SIZE_MISMATCH"
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_to_mega_upload_error_is_not_retried_after_upload_started(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, store = _setup(
        tmp_path,
        source_type="SEAFILE",
        destination_type="MEGA",
        source_adapter="SEAFILE_API",
        destination_adapter="RCLONE",
    )
    mega = providers[transfer.destination_connection_id]
    mega.fail_mega_upload_after_write = True

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert mega.mega_upload_attempts == 1
    assert store.transfer_events[transfer.id][0]["provider_error_code"] == (
        "MEGA_DESTINATION_STATE_UNCERTAIN"
    )
    assert "/backup/source" in mega.data
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_skip_race_does_not_overwrite_or_report_renamed_file_as_success(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, store = _setup(
        tmp_path,
        conflict_policy="SKIP",
    )
    destination = providers[transfer.destination_connection_id]
    destination.race_on_upload = True

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert destination.upload_calls == [("/backup/source", False)]
    assert destination.data["/backup/source"] == b"concurrent writer"
    assert destination.data["/backup/source (1)"] == b"payload"
    assert store.transfer_events[transfer.id][0]["provider_error_code"] == (
        "SEAFILE_DESTINATION_NAME_CONFLICT"
    )
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_destination_accepts_seafiles_unicode_nfc_name_normalization(tmp_path):
    decomposed_name = unicodedata.normalize("NFD", "café.txt")
    source_path = f"/source/{decomposed_name}"
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path,
        source_tree={"/source": None, source_path: b"unicode"},
    )
    destination = providers[transfer.destination_connection_id]
    destination.normalize_names_on_upload = True

    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert destination.data["/backup/source/café.txt"] == b"unicode"
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_auth_failure_does_not_fallback_to_rclone(tmp_path):
    executor, transfer, providers, runner, _snapshots, staging, _cancel, store = _setup(tmp_path)
    providers[transfer.source_connection_id].fail_auth = True

    await executor.execute(transfer.id)

    assert transfer.status == "FAILED"
    assert transfer.files_completed == 0
    assert runner.downloads == []
    assert runner.uploads == []
    assert store.transfer_events[transfer.id][0]["provider_error_code"] == (
        "SEAFILE_API_AUTHENTICATION_FAILED"
    )
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_transfer_cancellation_cleans_private_staging(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, cancel_state, _store = _setup(
        tmp_path
    )
    source = providers[transfer.source_connection_id]

    async def cancel_after_download(remote_path, destination_dir, **_kwargs):
        destination_dir.write_bytes(source.data[remote_path])
        cancel_state["cancel"] = True
        raise SeafileApiCancelledError("cancelled")

    source.download_file = cancel_after_download

    await executor.execute(transfer.id)

    assert transfer.status == "CANCELLED"
    assert transfer.files_completed == 0
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_destination_creates_nested_and_empty_directories(tmp_path):
    tree = {
        "/source": None,
        "/source/nested": None,
        "/source/nested/deep": None,
        "/source/nested/deep/final": None,
        "/source/nested/deep/final/資料 résumé.txt": b"nested",
        "/source/empty": None,
        "/source/empty/deeper": None,
    }
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path, source_tree=tree
    )

    await executor.execute(transfer.id)

    destination = providers[transfer.destination_connection_id]
    assert {
        "/backup/source",
        "/backup/source/nested",
        "/backup/source/nested/deep",
        "/backup/source/nested/deep/final",
        "/backup/source/empty",
        "/backup/source/empty/deeper",
    }.issubset(destination.directories)
    assert destination.data["/backup/source/nested/deep/final/資料 résumé.txt"] == b"nested"
    assert transfer.status == "COMPLETED"
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_to_api_transfers_zero_byte_file_and_verifies_empty_hash(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path,
        source_paths=["/zero-byte.txt"],
        source_tree={"/zero-byte.txt": b""},
        destination_path="/archive",
    )

    await executor.execute(transfer.id)

    destination = providers[transfer.destination_connection_id]
    assert transfer.status == "COMPLETED"
    assert destination.data["/archive/zero-byte.txt"] == b""
    assert transfer.files_completed == transfer.files_total == 1
    assert transfer.bytes_transferred == 0
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "destination_type", "source_adapter", "destination_adapter"),
    [
        ("SEAFILE", "MEGA", "SEAFILE_API", "RCLONE"),
        ("MEGA", "SEAFILE", "RCLONE", "SEAFILE_API"),
    ],
)
async def test_api_involved_transfers_support_mega_sessions(
    tmp_path, source_type, destination_type, source_adapter, destination_adapter
):
    tree = {
        "/source": None,
        "/source/nested": None,
        "/source/nested/file.txt": b"payload",
    }
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path,
        source_type=source_type,
        destination_type=destination_type,
        source_adapter=source_adapter,
        destination_adapter=destination_adapter,
        source_tree=tree,
    )

    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert transfer.files_completed == 1
    assert providers[transfer.source_connection_id].open_count == (
        1 if source_type == "MEGA" else 0
    )
    assert providers[transfer.destination_connection_id].open_count == (
        1 if destination_type == "MEGA" else 0
    )
    destination = providers[transfer.destination_connection_id]
    assert destination.data["/backup/source/nested/file.txt"] == b"payload"
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_api_destination_overwrite_if_different_skips_matching_content(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path,
        destination_tree={"/backup/source": b"payload"},
    )
    destination = providers[transfer.destination_connection_id]

    await executor.execute(transfer.id)

    assert transfer.status == "COMPLETED"
    assert destination.upload_attempts == 0
    assert transfer.files_completed == 1
    assert transfer.bytes_transferred == 0
    assert list(staging.iterdir()) == []


@pytest.mark.asyncio
async def test_rclone_selected_for_both_connections_keeps_remote_to_remote_copy(tmp_path):
    executor, transfer, providers, _runner, _snapshots, staging, _cancel, _store = _setup(
        tmp_path,
        source_adapter="RCLONE",
        destination_adapter="RCLONE",
    )

    class DirectRunner(FakeRcloneRunner):
        def __init__(self, provider_map):
            super().__init__(provider_map)
            self.remote_copy_calls = []

        async def run(self, command, paths, config, job_id, **_kwargs):
            self.remote_copy_calls.append((command, paths, config, job_id))
            if command == "copy":
                source_provider, source_path = self._provider_path(paths[0])
                destination_provider, destination_root = self._provider_path(paths[1])
                if source_path in source_provider.data:
                    destination_path = (
                        f"{destination_root.rstrip('/')}/{source_path.rsplit('/', 1)[-1]}"
                    )
                    data = source_provider.data[source_path]
                    destination_provider.data[destination_path] = data
                    destination_provider.items[destination_path] = _item(
                        destination_path,
                        data=data,
                        provider=destination_provider.provider_type,
                    )
            return b"", b""

    direct_runner = DirectRunner(providers)
    executor.runner = direct_runner

    await executor.execute(transfer.id)

    source = providers[transfer.source_connection_id]
    destination = providers[transfer.destination_connection_id]
    assert transfer.status == "COMPLETED"
    assert len(direct_runner.remote_copy_calls) == 1
    command, paths, _config, _job_id = direct_runner.remote_copy_calls[0]
    assert command == "copy"
    assert paths == [source.remote_path("/source"), destination.remote_path("/backup")]
    assert list(staging.iterdir()) == []
