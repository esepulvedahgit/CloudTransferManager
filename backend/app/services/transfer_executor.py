import asyncio
import hashlib
import random
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.providers.base import StorageItem
from app.providers.megacmd import MegaCmdProvider
from app.providers.mock import MockStorageProvider
from app.providers.rclone import RcloneGoogleDriveProvider
from app.providers.seafile import SeafileRcloneProvider
from app.providers.seafile_api import (
    SeafileApiCancelledError,
    SeafileApiError,
    SeafileApiProvider,
)
from app.security.credentials import CredentialCipher
from app.services.megacmd import (
    MegaCmdCancelledError,
    MegaCmdCommandError,
    MegaCmdRunner,
    MegaCmdTimeoutError,
)
from app.services.memory_store import ConnectionRecord, TransferRecord
from app.services.rclone import RcloneCancelledError, RcloneCommandError, RcloneRunner
from app.services.staging import temporary_staging_area
from app.services.telemetry import parse_mega_progress_line, parse_rclone_stats_line
from app.settings import Settings


class TransferExecutor:
    """Runs mock vertical-slice or isolated MEGAcmd/Rclone copy transfers."""

    def __init__(
        self,
        settings: Settings,
        store: Any,
        publish: Callable[[TransferRecord], Awaitable[None]],
        should_cancel: Callable[[], Awaitable[bool]],
        *,
        runner: RcloneRunner | Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.publish = publish
        self.should_cancel = should_cancel
        self.runner = runner or RcloneRunner(
            settings.rclone_binary,
            settings.transfer_temp_path,
            settings.transfer_staging_path,
        )
        self.sleep = sleep

    async def execute(self, transfer_id: uuid.UUID) -> None:
        record = await self.store.aget_transfer(transfer_id)
        if record is None or record.status in self._terminal:
            return
        source = await self.store.aget_connection(record.source_connection_id)
        destination = await self.store.aget_connection(record.destination_connection_id)
        if source is None or destination is None:
            await self._fail(record, error_code="CONNECTION_NOT_FOUND")
            return

        record.status = "VALIDATING"
        record.started_at = datetime.now(UTC)
        await self.publish(record)
        if await self.should_cancel():
            await self._cancel(record)
            return
        if source.provider_type == destination.provider_type == "MOCK":
            await self._run_mock(record)
            return
        if source.provider_type == "MOCK" or destination.provider_type == "MOCK":
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return

        try:
            await self._run_rclone(record, source, destination)
        except (RcloneCancelledError, MegaCmdCancelledError, SeafileApiCancelledError):
            await self._cancel(record)
        except SeafileApiError as exc:
            await self._fail(
                record,
                error_code=self._seafile_error_code(exc),
                technical_details={"http_status": exc.status_code},
            )
        except Exception as exc:
            await self._fail(
                record,
                error_code="TRANSFER_EXECUTION_FAILED",
                technical_details={"exception_type": type(exc).__name__},
            )

    @property
    def _terminal(self) -> set[str]:
        return {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}

    async def _run_mock(self, record: TransferRecord) -> None:
        provider = MockStorageProvider()
        try:
            files: list[Any] = []
            for path in record.source_paths:
                files.extend(
                    await self._walk_mock(provider, MockStorageProvider.normalize_path(path))
                )
        except ValueError:
            await self._fail(record)
            return
        record.files_total = len(files)
        record.bytes_total = sum(item.size_bytes or 0 for item in files)
        record.status = "RUNNING"
        await self.publish(record)
        for item in files:
            if await self.should_cancel():
                await self._cancel(record)
                return
            record.current_item = item.path
            await self.sleep(0.05)
            record.files_completed += 1
            record.bytes_transferred += item.size_bytes or 0
            record.progress_percent = (
                round(record.bytes_transferred * 100 / record.bytes_total, 2)
                if record.bytes_total
                else round(record.files_completed * 100 / max(record.files_total, 1), 2)
            )
            record.average_speed_bps = item.size_bytes or 0
            record.current_speed_bps = item.size_bytes or 0
            await self.publish(record)
        record.current_item = None
        record.current_speed_bps = None
        record.progress_percent = 100.0
        record.status = "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _walk_mock(self, provider: MockStorageProvider, path: str) -> list[Any]:
        item = await provider.get_item(path)
        if item is None:
            raise ValueError("Source item does not exist")
        if not item.is_dir:
            return [item]
        page = await provider.list_items(path, page_size=200)
        items: list[Any] = []
        for child in page.items:
            items.extend(await self._walk_mock(provider, child.path))
        return items

    def _provider(self, connection: ConnectionRecord) -> Any:
        if not connection.encrypted_credentials:
            raise ValueError("Missing provider credentials")
        credentials = CredentialCipher.from_secret_file(
            self.settings.master_encryption_key_file
        ).decrypt(connection.id, connection.encrypted_credentials)
        if connection.provider_type == "MEGA":
            return MegaCmdProvider(
                connection.id,
                credentials,
                MegaCmdRunner(self.settings.megacmd_binary_dir),
                Path(self.settings.transfer_staging_path),
            )
        if connection.provider_type == "GOOGLE_DRIVE":
            return RcloneGoogleDriveProvider(
                connection.id,
                credentials,
                self.runner,
            )
        if connection.provider_type == "SEAFILE":
            if connection.adapter_type == "SEAFILE_API":
                return SeafileApiProvider(connection.id, credentials)
            return SeafileRcloneProvider(connection.id, credentials, self.runner)
        raise ValueError("Unsupported provider")

    async def _run_rclone(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
    ) -> None:
        source = self._provider(source_connection)
        destination = self._provider(destination_connection)
        if self._uses_seafile_api(source_connection) or self._uses_seafile_api(
            destination_connection
        ):
            await self._run_seafile_api_cross_provider(
                record,
                source_connection,
                destination_connection,
                source,
                destination,
            )
            return
        if (
            source_connection.provider_type == "MEGA"
            or destination_connection.provider_type == "MEGA"
        ):
            await self._run_mega_cross_provider(
                record,
                source_connection,
                destination_connection,
                source,
                destination,
            )
            return

        source_items: list[Any] = []
        for path in record.source_paths:
            item = await source.get_item(path)
            if item is None:
                await self._fail(record, error_code="SOURCE_ITEM_NOT_FOUND")
                return
            source_items.append(item)

        path_count = max(len(source_items), 1)
        file_count_available = bool(source_items) and all(not item.is_dir for item in source_items)
        byte_weights_available = bool(source_items) and all(
            not item.is_dir and item.size_bytes is not None for item in source_items
        )
        if file_count_available:
            record.files_total = path_count
        path_weights = (
            [max(item.size_bytes or 0, 0) for item in source_items]
            if byte_weights_available
            else [1] * path_count
        )
        total_progress_weight = sum(path_weights)
        if total_progress_weight <= 0:
            path_weights = [1] * path_count
            total_progress_weight = path_count
            byte_weights_available = False
        elif byte_weights_available:
            record.bytes_total = total_progress_weight
        config_content = source.config_content().rstrip() + "\n" + destination.config_content()

        for attempt in range(1, self.settings.transfer_max_retries + 2):
            if await self.should_cancel():
                await self._cancel(record)
                return
            record.attempt_count = attempt
            record.status = "RUNNING" if attempt == 1 else "RETRYING"
            if attempt > 1:
                record.error_count += 1
            await self.publish(record)
            byte_offset = 0
            file_offset = 0
            try:
                accumulated_weight = 0
                for path_index, path in enumerate(record.source_paths):
                    if await self.should_cancel():
                        await self._cancel(record)
                        return

                    async def on_line(
                        line: str,
                        byte_base: int = byte_offset,
                        file_base: int = file_offset,
                        path_count_for_job: int = path_count,
                        path_weight_base: int = accumulated_weight,
                        path_weight_for_job: int = path_weights[path_index],
                        total_weight_for_job: int = total_progress_weight,
                        use_byte_weights: bool = byte_weights_available,
                    ) -> None:
                        nonlocal byte_offset, file_offset
                        progress = parse_rclone_stats_line(line)
                        if progress is None:
                            return
                        if "bytes_transferred" in progress:
                            record.bytes_transferred = byte_base + progress["bytes_transferred"]
                        if "bytes_total" in progress and not use_byte_weights:
                            record.bytes_total = byte_base + progress["bytes_total"]
                        if "files_completed" in progress:
                            record.files_completed = file_base + progress["files_completed"]
                        if "files_total" in progress and not file_count_available:
                            record.files_total = file_base + progress["files_total"]
                        if "current_speed_bps" in progress:
                            record.current_speed_bps = progress["current_speed_bps"]
                            elapsed = max(
                                (
                                    datetime.now(UTC) - (record.started_at or datetime.now(UTC))
                                ).total_seconds(),
                                0.0,
                            )
                            if elapsed > 0:
                                record.average_speed_bps = int(record.bytes_transferred / elapsed)
                        record.estimated_remaining_seconds = progress.get(
                            "estimated_remaining_seconds"
                        )
                        if use_byte_weights and record.current_speed_bps:
                            remaining_bytes = max(
                                total_weight_for_job - record.bytes_transferred, 0
                            )
                            record.estimated_remaining_seconds = (
                                remaining_bytes + record.current_speed_bps - 1
                            ) // record.current_speed_bps
                        elif path_count_for_job > 1:
                            # Without workload weights, a per-path ETA is not a reliable
                            # whole-job estimate, so omit it for the multi-path transfer.
                            record.estimated_remaining_seconds = None
                        local_progress = progress.get("progress_percent")
                        if not isinstance(local_progress, (int, float)):
                            current_bytes = progress.get("bytes_transferred")
                            total_bytes = progress.get("bytes_total")
                            current_files = progress.get("files_completed")
                            total_files = progress.get("files_total")
                            if (
                                isinstance(current_bytes, int)
                                and isinstance(total_bytes, int)
                                and total_bytes
                            ):
                                local_progress = current_bytes * 100 / total_bytes
                            elif (
                                isinstance(current_files, int)
                                and isinstance(total_files, int)
                                and total_files
                            ):
                                local_progress = current_files * 100 / total_files
                        if isinstance(local_progress, (int, float)):
                            clamped_local_progress = min(max(local_progress, 0), 100)
                            aggregate_progress = (
                                (
                                    path_weight_base
                                    + path_weight_for_job * clamped_local_progress / 100
                                )
                                * 100
                                / total_weight_for_job
                            )
                            record.progress_percent = max(
                                record.progress_percent,
                                min(round(aggregate_progress, 2), 99.99),
                            )
                        record.current_item = progress.get("current_item")
                        await self.publish(record)

                    source_item = source_items[path_index]
                    destination_path = MockStorageProvider.normalize_path(
                        record.destination_path
                    )
                    if source_item.is_dir:
                        selected_folder_name = PurePosixPath(
                            MockStorageProvider.normalize_path(path)
                        ).name
                        if selected_folder_name:
                            destination_path = MockStorageProvider.normalize_path(
                                f"{destination_path.rstrip('/')}/{selected_folder_name}"
                            )

                    await self.runner.run(
                        "copy",
                        [
                            source.remote_path(path),
                            destination.remote_path(destination_path),
                        ],
                        config_content,
                        record.id,
                        line_callback=on_line,
                        should_cancel=self.should_cancel,
                        conflict_policy=record.conflict_policy,
                        create_empty_src_dirs=source_item.is_dir,
                    )
                    byte_offset = record.bytes_transferred
                    file_offset = record.files_completed
                    accumulated_weight += path_weights[path_index]
                break
            except RcloneCancelledError:
                raise
            except RcloneCommandError as exc:
                if not exc.retryable or attempt > self.settings.transfer_max_retries:
                    await self._fail(
                        record,
                        error_code=(
                            "RCLONE_RETRIES_EXHAUSTED" if exc.retryable else "RCLONE_COMMAND_FAILED"
                        ),
                        message="No se pudo completar la copia con el proveedor.",
                        technical_details={
                            "return_code": exc.return_code,
                            "retryable": exc.retryable,
                            "attempt": attempt,
                        },
                    )
                    return
                record.status = "RETRYING"
                await self.publish(record)
                delay = min(5 * (2 ** (attempt - 1)) + random.random(), 300)
                if not await self._wait_for_retry(record, delay):
                    return
        else:
            await self._fail(record)
            return

        record.current_item = None
        record.current_speed_bps = None
        record.progress_percent = 100.0
        record.status = "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    @staticmethod
    def _uses_seafile_api(connection: ConnectionRecord) -> bool:
        return connection.provider_type == "SEAFILE" and connection.adapter_type == "SEAFILE_API"

    async def _run_seafile_api_cross_provider(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
        source: Any,
        destination: Any,
    ) -> None:
        """Stage API-involved copies one file at a time; never switch adapters on errors."""
        source_is_mega = source_connection.provider_type == "MEGA"
        destination_is_mega = destination_connection.provider_type == "MEGA"
        api_destination = self._uses_seafile_api(destination_connection)
        destination_is_rclone = (
            destination_connection.provider_type
            in {
                "GOOGLE_DRIVE",
                "SEAFILE",
            }
            and not api_destination
        )

        async with AsyncExitStack() as stack:
            mega_session = None
            if source_is_mega or destination_is_mega:
                mega_provider = source if source_is_mega else destination
                mega_session = await stack.enter_async_context(
                    mega_provider.open_session(
                        transfer_id=record.id,
                        should_cancel=self.should_cancel,
                    )
                )

            source_listing = mega_session if source_is_mega else source
            try:
                files, directories = await self._collect_files(
                    source_listing,
                    record.source_paths,
                    include_selected_folder=True,
                )
            except FileNotFoundError:
                await self._fail(record, error_code="SOURCE_ITEM_NOT_FOUND")
                return

            record.files_total = len(files)
            sizes_known = all(item.size_bytes is not None for item, _path in files)
            record.bytes_total = sum(item.size_bytes or 0 for item, _path in files)
            record.bytes_transferred = 0
            record.files_completed = 0
            record.status = "RUNNING"
            await self.publish(record)

            destination_root = MockStorageProvider.normalize_path(record.destination_path)
            if directories:
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                ordered_directories = sorted(
                    directories,
                    key=lambda value: (len(PurePosixPath(value).parts), value),
                )
                if api_destination:
                    for relative_directory in ordered_directories:
                        if await self.should_cancel():
                            await self._cancel(record)
                            return
                        safe_relative = self._safe_relative_path(relative_directory)
                        target = MockStorageProvider.normalize_path(
                            f"{destination_root.rstrip('/')}/{safe_relative}"
                        )
                        await destination.mkdir(target, should_cancel=self.should_cancel)
                elif destination_is_mega:
                    assert mega_session is not None
                    for relative_directory in ordered_directories:
                        if await self.should_cancel():
                            await self._cancel(record)
                            return
                        safe_relative = self._safe_relative_path(relative_directory)
                        target = MockStorageProvider.normalize_path(
                            f"{destination_root.rstrip('/')}/{safe_relative}"
                        )
                        await mega_session.mkdir(target)
                elif destination_is_rclone:
                    async with temporary_staging_area(
                        Path(self.settings.transfer_staging_path),
                        transfer_id=record.id,
                        expected_size=0,
                    ) as staging_area:
                        for relative_directory in ordered_directories:
                            safe_relative = self._safe_relative_path(relative_directory)
                            staging_area.joinpath(*PurePosixPath(safe_relative).parts).mkdir(
                                parents=True, exist_ok=True
                            )
                        await self.runner.copy_local_directory_to_remote(
                            staging_area,
                            destination.remote_path(destination_root),
                            destination.config_content(),
                            record.id,
                            should_cancel=self.should_cancel,
                            conflict_policy=record.conflict_policy,
                        )

            for item, relative_path in files:
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                safe_relative = self._safe_relative_path(relative_path)
                relative = PurePosixPath(safe_relative)
                relative_parent = relative.parent.as_posix()
                destination_folder = (
                    destination_root
                    if relative_parent == "."
                    else MockStorageProvider.normalize_path(
                        f"{destination_root.rstrip('/')}/{relative_parent}"
                    )
                )
                destination_file = MockStorageProvider.normalize_path(
                    f"{destination_folder.rstrip('/')}/{relative.name}"
                )

                if destination_is_mega:
                    assert mega_session is not None
                    if await mega_session.get_item(destination_file) is not None:
                        record.warning_count += 1
                        record.files_completed += 1
                        self._update_transfer_progress(record, files, completed_file=True)
                        await self.publish(record)
                        continue
                elif destination_is_rclone:
                    existing = await destination.get_item(destination_file)
                    if existing is not None and record.conflict_policy == "SKIP":
                        record.warning_count += 1
                        record.files_completed += 1
                        self._update_transfer_progress(record, files, completed_file=True)
                        await self.publish(record)
                        continue
                    if existing is not None and existing.is_dir:
                        await self._fail(record, error_code="DESTINATION_PATH_IS_DIRECTORY")
                        return
                elif api_destination:
                    existing = await destination.get_item(destination_file)
                    if existing is not None and record.conflict_policy == "SKIP":
                        record.warning_count += 1
                        record.files_completed += 1
                        self._update_transfer_progress(record, files, completed_file=True)
                        await self.publish(record)
                        continue
                    if existing is not None and existing.is_dir:
                        await self._fail(record, error_code="DESTINATION_PATH_IS_DIRECTORY")
                        return

                record.current_item = item.path
                record.current_speed_bps = None
                record.estimated_remaining_seconds = None
                await self.publish(record)
                byte_base = record.bytes_transferred
                completed = False
                already_identical = False
                size_added_for_file = False
                staged_file_size = 0
                mega_upload_started = False
                for attempt in range(1, self.settings.transfer_max_retries + 2):
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    record.attempt_count = max(record.attempt_count, attempt)
                    record.status = "RUNNING" if attempt == 1 else "RETRYING"
                    if attempt > 1:
                        record.bytes_transferred = byte_base
                        await self.publish(record)
                    local_file_bytes = 0

                    async def on_progress(
                        line: str,
                        byte_base_for_file: int = byte_base,
                    ) -> None:
                        nonlocal local_file_bytes
                        metrics = parse_rclone_stats_line(line)
                        if metrics is None:
                            metrics = parse_mega_progress_line(line)
                        if metrics is None:
                            return
                        current_bytes = metrics.get("bytes_transferred")
                        if isinstance(current_bytes, int):
                            local_file_bytes = max(local_file_bytes, current_bytes)
                            record.bytes_transferred = byte_base_for_file + local_file_bytes
                        speed = metrics.get("current_speed_bps")
                        if isinstance(speed, int):
                            record.current_speed_bps = speed
                        if record.bytes_total and record.bytes_transferred < record.bytes_total:
                            record.progress_percent = max(
                                record.progress_percent,
                                min(
                                    round(record.bytes_transferred * 100 / record.bytes_total, 2),
                                    99.99,
                                ),
                            )
                        await self.publish(record)

                    async def on_api_progress(
                        current: int,
                        total: int | None,
                        byte_base_for_api: int = byte_base,
                        item_for_api: StorageItem = item,
                        completed_files: int = record.files_completed,
                    ) -> None:
                        nonlocal local_file_bytes
                        local_file_bytes = max(local_file_bytes, current)
                        record.bytes_transferred = byte_base_for_api + local_file_bytes
                        if total is not None and item_for_api.size_bytes is None:
                            remaining = sum(
                                remaining_item.size_bytes or 0
                                for remaining_item, _path in files[completed_files + 1 :]
                            )
                            record.bytes_total = max(
                                record.bytes_total,
                                byte_base_for_api + total + remaining,
                            )
                        if sizes_known and record.bytes_total:
                            record.progress_percent = max(
                                record.progress_percent,
                                min(
                                    round(
                                        record.bytes_transferred * 100 / record.bytes_total,
                                        2,
                                    ),
                                    99.99,
                                ),
                            )
                        elif files:
                            expected_size = item_for_api.size_bytes or total or 0
                            file_progress = (
                                min(local_file_bytes / expected_size, 1.0)
                                if expected_size > 0
                                else 0.0
                            )
                            aggregate_progress = (
                                (record.files_completed + file_progress)
                                * 100
                                / max(record.files_total, 1)
                            )
                            record.progress_percent = max(
                                record.progress_percent,
                                min(round(aggregate_progress, 2), 99.99),
                            )
                        await self.publish(record)

                    retryable = False
                    try:
                        async with temporary_staging_area(
                            Path(self.settings.transfer_staging_path),
                            transfer_id=record.id,
                            expected_size=item.size_bytes,
                        ) as staging_area:
                            if self._uses_seafile_api(source_connection):
                                local_file = staging_area / item.name
                                await source.download_file(
                                    item.path,
                                    local_file,
                                    should_cancel=self.should_cancel,
                                    progress_callback=on_api_progress,
                                )
                            elif source_is_mega:
                                assert mega_session is not None
                                local_file = await mega_session.download_file(
                                    item.path,
                                    staging_area,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                            else:
                                await self.runner.copy_remote_to_local(
                                    source.remote_path(item.path),
                                    staging_area,
                                    source.config_content(),
                                    record.id,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                                local_file = staging_area / item.name

                            file_size = local_file.stat().st_size
                            staged_file_size = file_size
                            if (
                                self._uses_seafile_api(source_connection)
                                and item.size_bytes is not None
                                and file_size != item.size_bytes
                            ):
                                await self._fail(
                                    record,
                                    error_code="SOURCE_SIZE_MISMATCH",
                                    message=(
                                        "El tamaño descargado no coincide con los metadatos "
                                        "del archivo de origen."
                                    ),
                                    technical_details={
                                        "expected_bytes": item.size_bytes,
                                        "downloaded_bytes": file_size,
                                    },
                                )
                                return
                            source_digest = await _sha256_file(local_file, self.should_cancel)
                            if item.size_bytes is None and not size_added_for_file:
                                remaining_known_size = sum(
                                    next_item.size_bytes or 0
                                    for next_item, _path in files[record.files_completed + 1 :]
                                )
                                record.bytes_total = max(
                                    record.bytes_total,
                                    byte_base + file_size + remaining_known_size,
                                )
                                size_added_for_file = True
                            if api_destination:
                                if (
                                    existing is not None
                                    and record.conflict_policy == "OVERWRITE_IF_DIFFERENT"
                                ):
                                    destination_digest = await destination.sha256(
                                        destination_file,
                                        should_cancel=self.should_cancel,
                                    )
                                    if destination_digest == source_digest:
                                        completed = True
                                        already_identical = True
                                        break
                                stored_path = await destination.upload_file(
                                    local_file,
                                    destination_file,
                                    replace=record.conflict_policy != "SKIP",
                                    should_cancel=self.should_cancel,
                                    progress_callback=on_api_progress,
                                )
                                destination_digest = await destination.sha256(
                                    stored_path,
                                    should_cancel=self.should_cancel,
                                )
                                if destination_digest != source_digest:
                                    await self._fail(
                                        record,
                                        error_code="SEAFILE_INTEGRITY_MISMATCH",
                                        message=(
                                            "La verificación de integridad de Seafile no coincide."
                                        ),
                                        technical_details={"path": relative.as_posix()},
                                    )
                                    return
                                stored_nfc = unicodedata.normalize("NFC", stored_path)
                                requested_nfc = unicodedata.normalize("NFC", destination_file)
                                if stored_nfc != requested_nfc:
                                    await self._fail(
                                        record,
                                        error_code="SEAFILE_DESTINATION_NAME_CONFLICT",
                                        message=(
                                            "Seafile guardó el archivo con otro nombre por un "
                                            "conflicto concurrente. Revise el destino antes "
                                            "de reintentar."
                                        ),
                                        technical_details={
                                            "requested_path": destination_file,
                                            "stored_path": stored_path,
                                        },
                                    )
                                    return
                            elif destination_is_mega:
                                assert mega_session is not None
                                mega_upload_started = True
                                await mega_session.upload_file(
                                    local_file,
                                    destination_folder,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                            else:
                                await self.runner.copy_local_to_remote(
                                    local_file,
                                    destination.remote_path(destination_folder),
                                    destination.config_content(),
                                    record.id,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                    conflict_policy=record.conflict_policy,
                                )
                        completed = True
                        break
                    except (RcloneCancelledError, MegaCmdCancelledError, SeafileApiCancelledError):
                        raise
                    except (
                        RcloneCommandError,
                        MegaCmdCommandError,
                        MegaCmdTimeoutError,
                        SeafileApiError,
                    ) as exc:
                        if mega_upload_started:
                            destination_visible: bool | None = None
                            try:
                                assert mega_session is not None
                                destination_visible = (
                                    await mega_session.get_item(destination_file) is not None
                                )
                            except Exception:
                                pass
                            await self._fail(
                                record,
                                error_code="MEGA_DESTINATION_STATE_UNCERTAIN",
                                message=(
                                    "No se reintentó la subida: MEGA puede haber recibido "
                                    "el archivo aunque el cliente informara un fallo. "
                                    "Revise el destino antes de reintentar."
                                ),
                                technical_details={
                                    "attempt": attempt,
                                    "retryable": self._is_retryable_transfer_error(exc),
                                    "destination_visible": destination_visible,
                                },
                            )
                            return
                        retryable = self._is_retryable_transfer_error(exc)
                        if not retryable or attempt > self.settings.transfer_max_retries:
                            code = (
                                self._seafile_error_code(exc)
                                if isinstance(exc, SeafileApiError)
                                else (
                                    "RCLONE_RETRIES_EXHAUSTED"
                                    if isinstance(exc, RcloneCommandError) and retryable
                                    else "RCLONE_COMMAND_FAILED"
                                    if isinstance(exc, RcloneCommandError)
                                    else "MEGACMD_RETRIES_EXHAUSTED"
                                    if retryable
                                    else "MEGACMD_COMMAND_FAILED"
                                )
                            )
                            await self._fail(
                                record,
                                error_code=code,
                                message="No se pudo completar la copia con el proveedor.",
                                technical_details={
                                    "attempt": attempt,
                                    "retryable": retryable,
                                    "http_status": exc.status_code
                                    if isinstance(exc, SeafileApiError)
                                    else None,
                                },
                            )
                            return
                        record.status = "RETRYING"
                        record.error_count += 1
                        record.current_speed_bps = None
                        record.estimated_remaining_seconds = None
                        await self.publish(record)
                        delay = (
                            exc.retry_after
                            if isinstance(exc, SeafileApiError) and exc.retry_after is not None
                            else min(5 * (2 ** (attempt - 1)) + random.random(), 300)
                        )
                        if not await self._wait_for_retry(record, delay):
                            return
                    if completed:
                        break

                if not completed:
                    await self._fail(record)
                    return
                actual_size = (
                    item.size_bytes if item.size_bytes is not None else staged_file_size
                )
                if not already_identical:
                    record.bytes_transferred = max(
                        record.bytes_transferred,
                        byte_base + actual_size,
                    )
                record.files_completed += 1
                self._update_transfer_progress(record, files, completed_file=True)
                record.current_speed_bps = None
                record.estimated_remaining_seconds = None
                await self.publish(record)

        record.current_item = None
        record.current_speed_bps = None
        record.estimated_remaining_seconds = None
        record.progress_percent = 100.0
        record.status = "COMPLETED_WITH_WARNINGS" if record.warning_count else "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    @staticmethod
    def _safe_relative_path(path: str) -> str:
        relative = PurePosixPath(path)
        if (
            not path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in path
            or "\x00" in path
        ):
            raise ValueError("Unsafe relative transfer path")
        return relative.as_posix()

    @staticmethod
    def _seafile_error_code(error: SeafileApiError) -> str:
        if error.status_code in {401, 403}:
            return "SEAFILE_API_AUTHENTICATION_FAILED"
        return "SEAFILE_API_RETRIES_EXHAUSTED" if error.retryable else "SEAFILE_API_REQUEST_FAILED"

    async def _run_mega_cross_provider(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
        source: Any,
        destination: Any,
    ) -> None:
        if source_connection.provider_type == destination_connection.provider_type:
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return
        mega_is_source = source_connection.provider_type == "MEGA"
        mega_provider = source if mega_is_source else destination
        drive_provider = destination if mega_is_source else source

        async with AsyncExitStack() as stack:
            mega_session = await stack.enter_async_context(
                mega_provider.open_session(
                    transfer_id=record.id,
                    should_cancel=self.should_cancel,
                )
            )
            source_listing = mega_session if mega_is_source else source
            try:
                files, directories = await self._collect_files(
                    source_listing,
                    record.source_paths,
                    include_selected_folder=True,
                )
            except FileNotFoundError:
                await self._fail(record, error_code="SOURCE_ITEM_NOT_FOUND")
                return

            record.files_total = len(files)
            sizes_known = all(item.size_bytes is not None for item, _relative in files)
            record.bytes_total = sum(item.size_bytes or 0 for item, _relative in files)
            record.status = "RUNNING"
            await self.publish(record)

            drive_config = drive_provider.config_content()
            record.bytes_transferred = 0
            destination_root = MockStorageProvider.normalize_path(record.destination_path)
            if mega_is_source and directories:
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                record.current_item = record.source_paths[0]
                await self.publish(record)
                async with temporary_staging_area(
                    Path(self.settings.transfer_staging_path),
                    transfer_id=record.id,
                    expected_size=0,
                ) as staging_area:
                    for directory_path in sorted(
                        directories,
                        key=lambda value: (len(PurePosixPath(value).parts), value),
                    ):
                        relative_directory = PurePosixPath(directory_path)
                        if relative_directory.is_absolute() or any(
                            part in {"", ".", ".."} for part in relative_directory.parts
                        ):
                            raise ValueError("Unsafe relative transfer path")
                        staging_area.joinpath(*relative_directory.parts).mkdir(
                            parents=True, exist_ok=True
                        )
                    await self.runner.copy_local_directory_to_remote(
                        staging_area,
                        drive_provider.remote_path(destination_root),
                        drive_config,
                        record.id,
                        should_cancel=self.should_cancel,
                        conflict_policy=record.conflict_policy,
                    )
                record.current_item = None
                await self.publish(record)
            elif directories:
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                record.current_item = record.source_paths[0]
                await self.publish(record)
                for directory_path in sorted(
                    directories,
                    key=lambda value: (len(PurePosixPath(value).parts), value),
                ):
                    relative_directory = PurePosixPath(directory_path)
                    if relative_directory.is_absolute() or any(
                        part in {"", ".", ".."} for part in relative_directory.parts
                    ):
                        raise ValueError("Unsafe relative transfer path")
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    destination_directory = MockStorageProvider.normalize_path(
                        f"{destination_root.rstrip('/')}/{relative_directory.as_posix()}"
                    )
                    await mega_session.mkdir(destination_directory)
                record.current_item = None
                await self.publish(record)

            for item, relative_path in files:
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                relative = PurePosixPath(relative_path)
                if relative.is_absolute() or any(
                    part in {"", ".", ".."} for part in relative.parts
                ):
                    raise ValueError("Unsafe relative transfer path")
                relative_parent = relative.parent.as_posix()
                destination_folder = (
                    destination_root
                    if relative_parent == "."
                    else MockStorageProvider.normalize_path(
                        f"{destination_root.rstrip('/')}/{relative_parent}"
                    )
                )
                destination_file = MockStorageProvider.normalize_path(
                    f"{destination_folder.rstrip('/')}/{relative.name}"
                )

                if not mega_is_source and await mega_session.get_item(destination_file) is not None:
                    record.warning_count += 1
                    record.files_completed += 1
                    self._update_transfer_progress(record, files, completed_file=True)
                    await self.publish(record)
                    continue

                record.current_item = item.path
                record.current_speed_bps = None
                record.estimated_remaining_seconds = None
                await self.publish(record)
                byte_base = record.bytes_transferred
                for attempt in range(1, self.settings.transfer_max_retries + 2):
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    record.attempt_count = max(record.attempt_count, attempt)
                    record.status = "RUNNING"
                    if attempt > 1:
                        record.bytes_transferred = byte_base
                        await self.publish(record)
                    local_file_bytes = 0
                    mega_upload_started = False

                    async def on_progress(
                        line: str,
                        byte_base_for_file: int = byte_base,
                        file_count_before: int = record.files_completed,
                    ) -> None:
                        nonlocal local_file_bytes
                        metrics = parse_rclone_stats_line(line)
                        if metrics is None:
                            metrics = parse_mega_progress_line(line)
                        if metrics is None:
                            return
                        current_bytes = metrics.get("bytes_transferred")
                        if isinstance(current_bytes, int):
                            local_file_bytes = max(local_file_bytes, current_bytes)
                            record.bytes_transferred = byte_base_for_file + local_file_bytes
                        current_total = metrics.get("bytes_total")
                        if not sizes_known and isinstance(current_total, int):
                            remaining_known = sum(
                                remaining.size_bytes or 0
                                for remaining, _path in files[file_count_before + 1 :]
                            )
                            record.bytes_total = max(
                                record.bytes_total,
                                byte_base_for_file + current_total + remaining_known,
                            )
                        speed = metrics.get("current_speed_bps")
                        if isinstance(speed, int):
                            record.current_speed_bps = speed
                            elapsed = max(
                                (
                                    datetime.now(UTC) - (record.started_at or datetime.now(UTC))
                                ).total_seconds(),
                                0.0,
                            )
                            if elapsed > 0:
                                record.average_speed_bps = int(record.bytes_transferred / elapsed)
                        eta = metrics.get("estimated_remaining_seconds")
                        if isinstance(eta, int):
                            record.estimated_remaining_seconds = eta
                        elif sizes_known and record.current_speed_bps:
                            record.estimated_remaining_seconds = (
                                max(record.bytes_total - record.bytes_transferred, 0)
                                // record.current_speed_bps
                            )
                        percent = metrics.get("progress_percent")
                        if isinstance(percent, (float, int)):
                            if sizes_known and record.bytes_total:
                                progress = record.bytes_transferred * 100 / record.bytes_total
                            else:
                                progress = (
                                    (
                                        record.files_completed
                                        + min(max(float(percent), 0), 100) / 100
                                    )
                                    * 100
                                    / max(record.files_total, 1)
                                )
                            record.progress_percent = max(
                                record.progress_percent, min(round(progress, 2), 99.99)
                            )
                        await self.publish(record)

                    try:
                        async with temporary_staging_area(
                            Path(self.settings.transfer_staging_path),
                            transfer_id=record.id,
                            expected_size=item.size_bytes,
                        ) as staging_area:
                            if mega_is_source:
                                local_file = await mega_session.download_file(
                                    item.path,
                                    staging_area,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                                await self.runner.copy_local_to_remote(
                                    local_file,
                                    drive_provider.remote_path(destination_folder),
                                    drive_config,
                                    record.id,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                    conflict_policy=record.conflict_policy,
                                )
                            else:
                                await self.runner.copy_remote_to_local(
                                    source.remote_path(item.path),
                                    staging_area,
                                    drive_config,
                                    record.id,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                                local_file = staging_area / item.name
                                mega_upload_started = True
                                await mega_session.upload_file(
                                    local_file,
                                    destination_folder,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                    except (RcloneCancelledError, MegaCmdCancelledError):
                        raise
                    except (
                        RcloneCommandError,
                        MegaCmdCommandError,
                        MegaCmdTimeoutError,
                    ) as exc:
                        retryable = self._is_retryable_transfer_error(exc)
                        if mega_upload_started:
                            destination_visible: bool | None = None
                            try:
                                destination_visible = (
                                    await mega_session.get_item(destination_file) is not None
                                )
                            except Exception:
                                pass
                            await self._fail(
                                record,
                                error_code="MEGA_DESTINATION_STATE_UNCERTAIN",
                                message=(
                                    "No se reintentó la subida: MEGA puede haber recibido "
                                    "el archivo aunque el cliente informara un fallo. "
                                    "Revise el destino antes de reintentar."
                                ),
                                technical_details={
                                    "attempt": attempt,
                                    "retryable": retryable,
                                    "destination_visible": destination_visible,
                                },
                            )
                            return
                        if not retryable or attempt > self.settings.transfer_max_retries:
                            provider_code = (
                                "RCLONE_RETRIES_EXHAUSTED"
                                if isinstance(exc, RcloneCommandError) and retryable
                                else "RCLONE_COMMAND_FAILED"
                                if isinstance(exc, RcloneCommandError)
                                else "MEGACMD_RETRIES_EXHAUSTED"
                                if retryable
                                else "MEGACMD_COMMAND_FAILED"
                            )
                            await self._fail(
                                record,
                                error_code=provider_code,
                                message="No se pudo completar la copia con el proveedor.",
                                technical_details={
                                    "return_code": getattr(exc, "return_code", None),
                                    "retryable": retryable,
                                    "attempt": attempt,
                                },
                            )
                            return
                        record.status = "RETRYING"
                        record.error_count += 1
                        record.current_speed_bps = None
                        record.estimated_remaining_seconds = None
                        await self.publish(record)
                        delay = min(5 * (2 ** (attempt - 1)) + random.random(), 300)
                        if not await self._wait_for_retry(record, delay):
                            return
                        continue
                    break

                if item.size_bytes is not None:
                    record.bytes_transferred = max(
                        record.bytes_transferred, byte_base + item.size_bytes
                    )
                record.files_completed += 1
                self._update_transfer_progress(record, files, completed_file=True)
                record.current_speed_bps = None
                record.estimated_remaining_seconds = None
                await self.publish(record)

        record.current_item = None
        record.current_speed_bps = None
        record.estimated_remaining_seconds = None
        record.progress_percent = 100.0
        record.status = "COMPLETED_WITH_WARNINGS" if record.warning_count else "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _collect_files(
        self,
        provider: Any,
        selected_paths: list[str],
        *,
        include_selected_folder: bool = False,
    ) -> tuple[list[tuple[StorageItem, str]], set[str]]:
        files: dict[str, tuple[StorageItem, str]] = {}
        directories: set[str] = set()
        visited_directories: set[str] = set()

        async def walk_directory(path: str, relative_directory: PurePosixPath) -> None:
            if await self.should_cancel():
                raise SeafileApiCancelledError("Transfer cancelled")
            if path in visited_directories:
                return
            visited_directories.add(path)
            if relative_directory.parts:
                directories.add(relative_directory.as_posix())
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                if await self.should_cancel():
                    raise SeafileApiCancelledError("Transfer cancelled")
                page = await provider.list_items(path, cursor=cursor, page_size=200)
                for child in page.items:
                    child_path = MockStorageProvider.normalize_path(child.path)
                    parent = child_path.rpartition("/")[0] or "/"
                    if parent != path:
                        raise ValueError("Provider returned an item outside the requested folder")
                    relative_child = relative_directory / child.name
                    if child.is_dir:
                        await walk_directory(child_path, relative_child)
                    else:
                        files.setdefault(child_path, (child, relative_child.as_posix()))
                if page.next_cursor is None:
                    break
                if page.next_cursor in seen_cursors:
                    raise ValueError("Provider returned a repeated page cursor")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor

        for requested_path in selected_paths:
            if await self.should_cancel():
                raise SeafileApiCancelledError("Transfer cancelled")
            normalized = MockStorageProvider.normalize_path(requested_path)
            item = await provider.get_item(normalized)
            if item is None:
                raise FileNotFoundError("Source item was not found")
            item_path = MockStorageProvider.normalize_path(item.path)
            if item_path != normalized:
                raise ValueError("Provider returned a different source path")
            if item.is_dir:
                selected_directory = (
                    PurePosixPath(item_path).name if include_selected_folder else "."
                )
                await walk_directory(item_path, PurePosixPath(selected_directory))
            else:
                files.setdefault(item_path, (item, item.name))
        return list(files.values()), directories

    @staticmethod
    def _update_transfer_progress(
        record: TransferRecord,
        files: list[tuple[StorageItem, str]],
        *,
        completed_file: bool,
    ) -> None:
        all_sizes_known = all(item.size_bytes is not None for item, _path in files)
        if all_sizes_known and record.bytes_total:
            progress = record.bytes_transferred * 100 / record.bytes_total
        else:
            progress = record.files_completed * 100 / max(record.files_total, 1)
        progress_cap = 100.0 if completed_file else 99.99
        record.progress_percent = max(
            record.progress_percent, min(round(progress, 2), progress_cap)
        )

    async def _wait_for_retry(self, record: TransferRecord, delay: float) -> bool:
        remaining = delay
        while remaining > 0:
            if await self.should_cancel():
                await self._cancel(record)
                return False
            interval = min(0.25, remaining)
            await self.sleep(interval)
            remaining -= interval
        return True

    @staticmethod
    def _is_retryable_transfer_error(
        error: RcloneCommandError | MegaCmdCommandError | MegaCmdTimeoutError | SeafileApiError,
    ) -> bool:
        return error.retryable

    async def _cancel(self, record: TransferRecord) -> None:
        record.status = "CANCELLED"
        record.current_item = None
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _fail(
        self,
        record: TransferRecord,
        *,
        error_code: str = "TRANSFER_EXECUTION_FAILED",
        message: str = "No se pudo completar la transferencia.",
        technical_details: dict[str, Any] | None = None,
    ) -> None:
        record.status = "FAILED"
        record.error_count += 1
        record.current_item = None
        record.current_speed_bps = None
        record.completed_at = datetime.now(UTC)
        await self.publish(record)
        await self.store.aadd_transfer_event(
            record.id,
            level="ERROR",
            event_type="FAILURE_DIAGNOSTIC",
            message=message,
            technical_details=technical_details or {},
            provider_error_code=error_code,
            correlation_id=record.id,
        )


async def _sha256_file(path: Path, should_cancel: Callable[[], Awaitable[bool]]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = await asyncio.to_thread(source.read, 1024 * 1024)
            if not chunk:
                break
            if await should_cancel():
                raise SeafileApiCancelledError("Seafile transfer cancelled")
            digest.update(chunk)
    return digest.hexdigest()
