import asyncio
import hashlib
import logging
import random
import time
import traceback
import unicodedata
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from app.providers.base import StorageItem
from app.providers.mock import MockStorageProvider
from app.providers.rclone import (
    GoogleDriveApiError,
    GoogleDriveCancelledError,
    RcloneGoogleDriveProvider,
    RcloneStorageProvider,
)
from app.providers.seafile_api import (
    SeafileApiCancelledError,
    SeafileApiError,
    SeafileApiProvider,
)
from app.services.megacmd import (
    MegaCmdCancelledError,
    MegaCmdCommandError,
    MegaCmdDownloadOutputMissingError,
    MegaCmdDownloadValidationError,
    MegaCmdError,
    MegaCmdListingError,
    MegaCmdTimeoutError,
)
from app.services.memory_store import (
    ConnectionRecord,
    TransferManifestItemRecord,
    TransferRecord,
)
from app.services.provider_factory import StorageProviderFactory
from app.services.rclone import (
    RcloneCancelledError,
    RcloneCommandError,
    RcloneExecutionProfile,
    RcloneRunner,
)
from app.services.source_inventory import (
    InventoryCancelledError,
    InventoryPreflightError,
    InventoryProgress,
    SourceInventoryScanner,
)
from app.services.staging import StagingCleanupError, StagingSpaceError, temporary_staging_area
from app.services.telemetry import parse_mega_progress_line, parse_rclone_stats_line
from app.services.transfer_naming import allocate_available_name, collision_key
from app.services.transfer_outcomes import CopyOutcome, FileCopyResult, SourceFileError
from app.services.transfer_progress import smb_manifest_progress_percent
from app.settings import Settings

logger = logging.getLogger(__name__)


def reserved_directory_names(parent_path: str, entries: list[dict[str, Any]]) -> set[str]:
    return {
        collision_key(entry["destination_path"].rsplit("/", 1)[-1])
        for entry in entries
        if entry.get("is_dir")
        and entry["destination_path"].rsplit("/", 1)[0] == parent_path.rstrip("/")
    }


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
        dispatch_token: uuid.UUID | None = None,
        defer_pause_finalize: bool = False,
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
        self.dispatch_token = dispatch_token
        self.defer_pause_finalize = defer_pause_finalize

    class _PauseAtFileBoundary(Exception):
        """Raised after the current file has settled and before selecting another one."""

    class _PreflightError(Exception):
        def __init__(
            self,
            error_code: str,
            message: str,
            technical_details: dict[str, Any] | None = None,
        ) -> None:
            super().__init__(message)
            self.error_code = error_code
            self.message = message
            self.technical_details = technical_details or {}

    async def _measure_stage(
        self,
        record: TransferRecord,
        *,
        code: str,
        connection: ConnectionRecord,
        role: str,
        manifest_item: TransferManifestItemRecord,
        operation: Callable[[], Awaitable[Any]],
        bytes_count: int | None = None,
    ) -> Any:
        started_at = datetime.now(UTC)
        started_tick = time.monotonic()
        span_id: uuid.UUID | None = None
        try:
            span_id = await self.store.astart_transfer_metric_span(
                record.id,
                dispatch_token=self.dispatch_token,
                attempt_count=record.attempt_count,
                span_type="STAGE",
                code=code,
                manifest_item_id=manifest_item.id,
                connection_id=connection.id,
                provider_type=connection.provider_type,
                connection_role=role,
                started_at=started_at,
            )
        except Exception:
            logger.warning("Could not start a transfer metric span")
        try:
            result = await operation()
        except BaseException as exc:
            if span_id is not None:
                outcome = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
                await self._finish_stage_metric(
                    span_id,
                    started_tick,
                    outcome=outcome,
                    bytes_count=None,
                )
            raise
        if span_id is not None:
            measured_bytes = bytes_count
            if measured_bytes is None and isinstance(result, Path):
                try:
                    measured_bytes = result.stat().st_size
                except OSError:
                    measured_bytes = None
            await self._finish_stage_metric(
                span_id,
                started_tick,
                outcome="SUCCESS",
                bytes_count=measured_bytes,
            )
        return result

    async def _finish_stage_metric(
        self,
        span_id: uuid.UUID,
        started_tick: float,
        *,
        outcome: str,
        bytes_count: int | None,
    ) -> None:
        try:
            await self.store.afinish_transfer_metric_span(
                span_id,
                dispatch_token=self.dispatch_token,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started_tick) * 1000)),
                outcome=outcome,
                bytes_count=bytes_count,
                error_code="TRANSFER_STAGE_FAILED" if outcome == "FAILED" else None,
            )
        except Exception:
            logger.warning("Could not finish a transfer metric span")

    @asynccontextmanager
    async def _measured_staging_area(
        self,
        record: TransferRecord,
        manifest_item: TransferManifestItemRecord,
        *,
        expected_size: int | None,
    ) -> AsyncIterator[Path]:
        span_id: uuid.UUID | None = None
        cleanup_tick: float | None = None
        outcome = "SUCCESS"
        try:
            async with temporary_staging_area(
                Path(self.settings.transfer_staging_path),
                transfer_id=record.id,
                expected_size=expected_size,
                reserve_bytes=self.settings.transfer_staging_reserve_bytes,
                margin_percent=self.settings.transfer_staging_margin_percent,
            ) as area:
                try:
                    yield area
                finally:
                    started_at = datetime.now(UTC)
                    try:
                        span_id = await self.store.astart_transfer_metric_span(
                            record.id,
                            dispatch_token=self.dispatch_token,
                            attempt_count=record.attempt_count,
                            span_type="STAGE",
                            code="STAGING_CLEANUP",
                            manifest_item_id=manifest_item.id,
                            started_at=started_at,
                        )
                    except Exception:
                        logger.warning("Could not start a transfer metric span")
                    if span_id is not None:
                        cleanup_tick = time.monotonic()
        except StagingCleanupError:
            outcome = "FAILED"
            raise
        finally:
            if span_id is not None and cleanup_tick is not None:
                await self._finish_stage_metric(
                    span_id,
                    cleanup_tick,
                    outcome=outcome,
                    bytes_count=None,
                )

    async def execute(self, transfer_id: uuid.UUID) -> bool:
        record = await self.store.aget_transfer(transfer_id)
        if record is None or record.status in self._terminal:
            return False
        source = await self.store.aget_connection(record.source_connection_id)
        destination = await self.store.aget_connection(record.destination_connection_id)
        if source is None or destination is None:
            await self._fail(record, error_code="CONNECTION_NOT_FOUND")
            return False
        record.pause_supported = bool(
            record.conflict_policy == "KEEP_BOTH"
            or source.provider_type == "SMB"
            or (source.provider_type == destination.provider_type == "MOCK")
        )
        record.pause_unavailable_reason = (
            None if record.pause_supported else "LEGACY_BULK_EXECUTION"
        )

        record.status = "VALIDATING"
        record.started_at = datetime.now(UTC)
        await self.publish(record)
        if await self.should_cancel():
            await self._cancel(record)
            return False
        if source.provider_type == destination.provider_type == "MOCK":
            try:
                await self._run_mock(record)
            except self._PauseAtFileBoundary:
                return await self._finish_pause_at_boundary(record)
            return False
        if source.provider_type == "MOCK" or destination.provider_type == "MOCK":
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return False

        uses_nextcloud = "NEXTCLOUD" in {source.provider_type, destination.provider_type}
        original_runner = self.runner
        if uses_nextcloud and isinstance(self.runner, RcloneRunner):
            self.runner = RcloneRunner(
                self.settings.rclone_binary,
                self.settings.transfer_temp_path,
                self.settings.transfer_staging_path,
                transfer_profile=RcloneExecutionProfile(
                    bandwidth_limit=self.settings.nextcloud_bandwidth_limit
                ),
            )

        try:
            async with asyncio.timeout(
                self.settings.nextcloud_job_timeout_seconds if uses_nextcloud else None
            ):
                if source.provider_type == "SMB":
                    if record.conflict_policy == "KEEP_BOTH":
                        await self._run_versioned_copy(record, source, destination)
                    else:
                        await self._run_smb_transfer(record, source, destination)
                elif record.conflict_policy == "KEEP_BOTH":
                    await self._run_versioned_copy(record, source, destination)
                else:
                    await self._run_rclone(record, source, destination)
        except self._PauseAtFileBoundary:
            return await self._finish_pause_at_boundary(record)
        except (
            RcloneCancelledError,
            MegaCmdCancelledError,
            SeafileApiCancelledError,
            GoogleDriveCancelledError,
            InventoryCancelledError,
        ):
            await self._cancel(record)
        except MegaCmdListingError as exc:
            await self._fail(
                record,
                error_code="MEGACMD_LISTING_INVALID",
                technical_details={
                    "exception_type": type(exc).__name__,
                    "provider_stage": "INVENTORY",
                },
            )
        except SeafileApiError as exc:
            await self._fail(
                record,
                error_code=self._seafile_error_code(exc),
                technical_details={"http_status": exc.status_code},
            )
        except Exception as exc:
            nextcloud_timeout = uses_nextcloud and isinstance(exc, TimeoutError)
            frames = traceback.extract_tb(exc.__traceback__)
            location = frames[-1] if frames else None
            details: dict[str, Any] = {"exception_type": type(exc).__name__}
            if location is not None:
                details["failure_location"] = {
                    "function": location.name[:128],
                    "line": location.lineno,
                }
            await self._fail(
                record,
                error_code=(
                    "NEXTCLOUD_TRANSFER_TIMEOUT"
                    if nextcloud_timeout
                    else "TRANSFER_EXECUTION_FAILED"
                ),
                message=(
                    "Se agotó el tiempo máximo de la transferencia."
                    if nextcloud_timeout
                    else "No se pudo completar la transferencia."
                ),
                technical_details=details,
            )
        finally:
            self.runner = original_runner
        return False

    async def _finish_pause_at_boundary(self, record: TransferRecord) -> bool:
        if self.defer_pause_finalize:
            return True
        paused = await self.store.acomplete_transfer_pause(
            record.id, self.dispatch_token, datetime.now(UTC)
        )
        if paused is not None:
            await self.publish(paused)
        return True

    async def _checkpoint_file_boundary(self, record: TransferRecord, next_item_path: str) -> None:
        checkpoint = getattr(self.store, "acheck_transfer_execution_control", None)
        if checkpoint is None:
            if await self.should_cancel():
                raise RcloneCancelledError("Transfer cancelled")
            return
        decision = await checkpoint(record.id, self.dispatch_token, next_item_path)
        if decision == "pause":
            raise self._PauseAtFileBoundary
        if decision == "cancel":
            raise RcloneCancelledError("Transfer cancelled")
        if decision == "lost":
            raise asyncio.CancelledError
        record.current_item = next_item_path

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
            await self._checkpoint_file_boundary(record, item.path)
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
        return StorageProviderFactory(self.settings).create(connection, runner=self.runner)

    async def _build_smb_manifest(self, record: TransferRecord, source: Any) -> None:
        """Discover a NAS tree incrementally, persisting each page before advancing."""
        selected: list[tuple[str, StorageItem]] = []
        for requested_path in record.source_paths:
            if await self.should_cancel():
                raise RcloneCancelledError("SMB inventory cancelled")
            normalized = MockStorageProvider.normalize_path(requested_path)
            item = await self._smb_source_call(source.get_item(normalized))
            if item is None:
                raise FileNotFoundError(normalized)
            item_path = MockStorageProvider.normalize_path(item.path)
            if item_path != normalized:
                raise ValueError("SMB source returned an unexpected path")
            selected.append((normalized, item))

        # Collapse duplicate and nested selections so each NAS subtree is inventoried once.
        collapsed: list[tuple[str, StorageItem]] = []
        for path, item in sorted(
            selected, key=lambda row: (len(PurePosixPath(row[0]).parts), row[0])
        ):
            if any(
                prior_item.is_dir
                and (path == prior_path or path.startswith(prior_path.rstrip("/") + "/"))
                for prior_path, prior_item in collapsed
            ):
                continue
            collapsed.append((path, item))

        destination_root = MockStorageProvider.normalize_path(record.destination_path)
        root_entries: list[dict[str, Any]] = []
        for source_path, item in collapsed:
            name = PurePosixPath(source_path).name
            destination_path = (
                destination_root
                if source_path == "/"
                else MockStorageProvider.normalize_path(f"{destination_root.rstrip('/')}/{name}")
            )
            root_entries.append(
                {
                    "source_path": source_path,
                    "destination_path": destination_path,
                    "is_dir": item.is_dir,
                    "size_bytes": item.size_bytes,
                    "modified_at": item.modified_at,
                }
            )
        destination_paths = [entry["destination_path"] for entry in root_entries]
        if len(destination_paths) != len(set(destination_paths)):
            raise self._PreflightError(
                "SMB_DESTINATION_PATH_COLLISION",
                "Dos elementos seleccionados apuntan a la misma ruta de destino. "
                "Ajuste la selección o el destino antes de iniciar.",
            )
        await self.store.aadd_transfer_manifest_items(record.id, root_entries)

        while True:
            if await self.should_cancel():
                raise RcloneCancelledError("SMB inventory cancelled")
            directories = await self.store.alist_pending_scan_directories(record.id, limit=1)
            if not directories:
                break
            directory = directories[0]
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                if await self.should_cancel():
                    raise RcloneCancelledError("SMB inventory cancelled")
                page = await self._smb_source_call(
                    source.list_items(directory.source_path, cursor=cursor, page_size=200)
                )
                entries: list[dict[str, Any]] = []
                for child in page.items:
                    name = child.name
                    if not name or name in {".", ".."} or "/" in name or "\\" in name:
                        raise ValueError("SMB returned an unsafe item name")
                    child_path = MockStorageProvider.normalize_path(
                        f"{directory.source_path.rstrip('/')}/{name}"
                    )
                    if MockStorageProvider.normalize_path(child.path) != child_path:
                        raise ValueError("SMB returned an item outside the requested folder")
                    entries.append(
                        {
                            "source_path": child_path,
                            "destination_path": MockStorageProvider.normalize_path(
                                f"{directory.destination_path.rstrip('/')}/{name}"
                            ),
                            "is_dir": child.is_dir,
                            "size_bytes": child.size_bytes,
                            "modified_at": child.modified_at,
                        }
                    )
                await self.store.aadd_transfer_manifest_items(record.id, entries)
                if page.next_cursor is None:
                    break
                if page.next_cursor in seen_cursors:
                    raise ValueError("SMB returned a repeated page cursor")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor
            await self.store.amark_transfer_directory_scanned(record.id, directory.id)
            current = await self.store.aget_transfer(record.id)
            if current is not None:
                record.files_total = current.files_total
                record.bytes_total = current.bytes_total
            record.current_item = directory.source_path
            await self.publish(record)

        current = await self.store.aget_transfer(record.id)
        if current is not None:
            record.files_total = current.files_total
            record.bytes_total = current.bytes_total
        if record.files_total > self.settings.smb_transfer_max_files:
            raise self._PreflightError(
                "TRANSFER_FILE_LIMIT_EXCEEDED",
                "La selección supera el máximo de archivos configurado para una transferencia SMB.",
                {"maximum_files": self.settings.smb_transfer_max_files},
            )
        cursor_id: uuid.UUID | None = None
        while True:
            page = await self.store.alist_transfer_manifest_items(
                record.id,
                statuses={"PENDING", "FAILED"},
                after_id=cursor_id,
                limit=200,
            )
            if not page:
                break
            for item in page:
                if item.size_bytes is None:
                    raise self._PreflightError(
                        "TRANSFER_FILE_SIZE_UNKNOWN",
                        "El NAS no informó el tamaño de todos los archivos; no se inició la copia.",
                        {"file_name": PurePosixPath(item.source_path).name},
                    )
                if (
                    self.settings.transfer_max_file_bytes
                    and item.size_bytes > self.settings.transfer_max_file_bytes
                ):
                    raise self._PreflightError(
                        "TRANSFER_FILE_SIZE_LIMIT_EXCEEDED",
                        "Un archivo supera el tamaño máximo permitido para una transferencia.",
                        {
                            "maximum_file_bytes": self.settings.transfer_max_file_bytes,
                            "file_bytes": item.size_bytes,
                        },
                    )
            cursor_id = page[-1].id

    async def _run_smb_transfer(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
    ) -> None:
        if destination_connection.provider_type not in {
            "GOOGLE_DRIVE",
            "MEGA",
            "SEAFILE",
            "NEXTCLOUD",
        }:
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return
        source = self._provider(source_connection)
        destination = self._provider(destination_connection)
        try:
            await self._build_smb_manifest(record, source)
        except FileNotFoundError:
            await self._fail(record, error_code="SOURCE_ITEM_NOT_FOUND")
            return
        except self._PreflightError as exc:
            await self._fail(
                record,
                error_code=exc.error_code,
                message=exc.message,
                technical_details=exc.technical_details,
            )
            return
        record = await self.store.aget_transfer(record.id) or record
        record.status = "RUNNING"
        record.current_item = None
        await self.publish(record)

        destination_is_mega = destination_connection.provider_type == "MEGA"
        destination_is_api = self._uses_seafile_api(destination_connection)
        mega_session = None
        async with AsyncExitStack() as stack:
            if destination_is_mega:
                mega_session = await stack.enter_async_context(
                    destination.open_session(
                        transfer_id=record.id,
                        should_cancel=self.should_cancel,
                    )
                )
            await self._create_smb_destination_directories(
                record, destination, mega_session=mega_session
            )

            cursor_id: uuid.UUID | None = None
            while True:
                page = await self.store.alist_transfer_manifest_items(
                    record.id,
                    statuses={"PENDING", "FAILED"},
                    after_id=cursor_id,
                    limit=200,
                )
                if not page:
                    break
                for item in page:
                    await self._checkpoint_file_boundary(record, item.source_path)
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    cursor_id = item.id
                    record.current_item = item.source_path
                    await self.publish(record)
                    try:
                        outcome = await self._copy_smb_manifest_file(
                            record,
                            source,
                            destination,
                            item,
                            destination_is_mega=destination_is_mega,
                            destination_is_api=destination_is_api,
                            mega_session=mega_session,
                        )
                        status, verified_by, error_code = outcome
                    except (RcloneCancelledError, MegaCmdCancelledError, SeafileApiCancelledError):
                        raise
                    except Exception as exc:
                        status, verified_by = "FAILED", None
                        error_code = (
                            self._seafile_error_code(exc)
                            if isinstance(exc, SeafileApiError)
                            else "TRANSFER_NETWORK_UNAVAILABLE"
                            if isinstance(exc, (RcloneCommandError, TimeoutError))
                            and (not isinstance(exc, RcloneCommandError) or exc.retryable)
                            else "SMB_FILE_COPY_FAILED"
                        )
                        technical_details: dict[str, Any] = {"exception_type": type(exc).__name__}
                        if isinstance(exc, RcloneCommandError):
                            technical_details.update(
                                return_code=exc.return_code,
                                retryable=exc.retryable,
                            )
                        await self.store.aadd_transfer_event(
                            record.id,
                            level="ERROR",
                            event_type="SMB_FILE_COPY_FAILED",
                            message="No se pudo copiar un archivo del NAS.",
                            technical_details=technical_details,
                            provider_error_code=error_code,
                            item_path=item.source_path,
                        )
                    await self.store.aupdate_transfer_manifest_item(
                        record.id,
                        item.id,
                        status=status,
                        verified_by=verified_by,
                        error_code=error_code,
                    )
                    if error_code == "DESTINATION_RECONCILIATION_REQUIRED":
                        await self.store.aadd_transfer_event(
                            record.id,
                            level="ERROR",
                            event_type="SMB_RECONCILIATION_REQUIRED",
                            message=(
                                "No se pudo verificar por contenido el archivo del destino "
                                "tras interrumpirse la copia. No se sobrescribió."
                            ),
                            provider_error_code=error_code,
                            item_path=item.source_path,
                        )
                    record = await self.store.aget_transfer(record.id) or record
                    record.current_item = item.source_path
                    record.status = "RUNNING"
                    await self.publish(record)

        counts = await self.store.aget_transfer_manifest_counts(record.id)
        record = await self.store.aget_transfer(record.id) or record
        record.current_item = None
        record.progress_percent = smb_manifest_progress_percent(
            bytes_transferred=record.bytes_transferred,
            bytes_total=record.bytes_total,
            files_total=record.files_total,
            counts=counts,
        )
        record.status = (
            "FAILED"
            if counts["failed"]
            else "COMPLETED_WITH_WARNINGS"
            if counts["skipped"]
            else "COMPLETED"
        )
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _create_smb_destination_directories(
        self,
        record: TransferRecord,
        destination: Any,
        *,
        mega_session: Any | None,
    ) -> None:
        root = MockStorageProvider.normalize_path(record.destination_path)
        if self._uses_seafile_api_from_provider(destination):
            mkdir = destination.mkdir
            async for directory in self._iterate_smb_directories(record.id):
                if await self.should_cancel():
                    raise SeafileApiCancelledError("Transfer cancelled")
                await mkdir(directory.destination_path, should_cancel=self.should_cancel)
            return
        if mega_session is not None:
            async for directory in self._iterate_smb_directories(record.id):
                if await self.should_cancel():
                    raise MegaCmdCancelledError("Transfer cancelled")
                await mega_session.mkdir(directory.destination_path)
            return
        if not root:
            return
        # One bounded local directory tree is enough for Rclone to create even
        # empty NAS folders. Each manifest page is released before fetching the next.
        async with temporary_staging_area(
            Path(self.settings.transfer_staging_path),
            transfer_id=record.id,
            expected_size=0,
        ) as staging_area:
            has_directories = False
            async for directory in self._iterate_smb_directories(record.id):
                has_directories = True
                relative = self._safe_destination_relative(root, directory.destination_path)
                if relative:
                    staging_area.joinpath(*PurePosixPath(relative).parts).mkdir(
                        parents=True, exist_ok=True
                    )
            if has_directories:
                await self.runner.copy_local_directory_to_remote(
                    staging_area,
                    destination.remote_path(root),
                    destination.config_content(),
                    record.id,
                    should_cancel=self.should_cancel,
                    conflict_policy=record.conflict_policy,
                )

    @staticmethod
    def _uses_seafile_api_from_provider(provider: Any) -> bool:
        return isinstance(provider, SeafileApiProvider)

    async def _smb_source_call(self, operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except (RcloneCommandError, TimeoutError, OSError) as exc:
            details = {"retryable": exc.retryable if isinstance(exc, RcloneCommandError) else True}
            raise self._PreflightError(
                "SMB_SOURCE_UNAVAILABLE",
                "No se pudo acceder al NAS durante el inventario. Compruebe la red local "
                "o la VPN externa y vuelva a intentarlo.",
                details,
            ) from None

    async def _iterate_smb_directories(
        self, transfer_id: uuid.UUID
    ) -> AsyncIterator[TransferManifestItemRecord]:
        cursor_id: uuid.UUID | None = None
        while True:
            page = await self.store.alist_transfer_manifest_items(
                transfer_id,
                statuses={"DIRECTORY"},
                after_id=cursor_id,
                limit=200,
                include_directories=True,
            )
            if not page:
                return
            for item in page:
                yield item
            cursor_id = page[-1].id

    @staticmethod
    def _safe_destination_relative(root: str, path: str) -> str:
        normalized_root = MockStorageProvider.normalize_path(root)
        normalized_path = MockStorageProvider.normalize_path(path)
        if normalized_path == normalized_root:
            return ""
        prefix = normalized_root.rstrip("/") + "/"
        if not normalized_path.startswith(prefix):
            raise ValueError("SMB destination path escaped selected destination")
        relative = normalized_path[len(prefix) :]
        return TransferExecutor._safe_relative_path(relative)

    async def _copy_smb_manifest_file(
        self,
        record: TransferRecord,
        source: Any,
        destination: Any,
        item: Any,
        *,
        destination_is_mega: bool,
        destination_is_api: bool,
        mega_session: Any | None,
    ) -> tuple[str, str | None, str | None]:
        try:
            source_item = await source.get_item(item.source_path)
        except (RcloneCommandError, TimeoutError, OSError):
            return "FAILED", None, "SMB_SOURCE_UNAVAILABLE"
        if source_item is None or source_item.is_dir:
            return "FAILED", None, "SOURCE_ITEM_NOT_FOUND"
        if source_item.size_bytes != item.size_bytes or not self._same_smb_modified_at(
            source_item.modified_at, item.modified_at
        ):
            return "FAILED", None, "SOURCE_CHANGED_DURING_MIGRATION"
        destination_item = await (
            mega_session.get_item(item.destination_path)
            if mega_session is not None
            else destination.get_item(item.destination_path)
        )
        if destination_item is not None:
            if destination_item.is_dir:
                return "FAILED", None, "DESTINATION_PATH_IS_DIRECTORY"
            if item.copy_started and not item.destination_existed_before:
                try:
                    verified = await self._verify_smb_interrupted_copy(
                        source,
                        destination,
                        item,
                        transfer_id=record.id,
                        destination_is_api=destination_is_api,
                        mega_session=mega_session,
                    )
                except (
                    RcloneCancelledError,
                    MegaCmdCancelledError,
                    SeafileApiCancelledError,
                ):
                    raise
                except Exception:
                    verified = False
                if verified:
                    return "COPIED", "SHA256", None
                return "FAILED", None, "DESTINATION_RECONCILIATION_REQUIRED"
            if record.conflict_policy == "SKIP" or destination_is_mega:
                if item.copy_started and not item.destination_existed_before:
                    return "FAILED", None, "DESTINATION_VERIFICATION_FAILED"
                return "SKIPPED", None, None
        if not item.copy_started:
            await self.store.amark_transfer_manifest_item_started(
                record.id,
                item.id,
                destination_existed_before=destination_item is not None,
            )
            item.copy_started = True
            item.destination_existed_before = destination_item is not None
        if not item.copy_started:
            await self.store.amark_transfer_manifest_item_started(
                record.id,
                item.id,
                destination_existed_before=destination_item is not None,
            )
            item.copy_started = True
            item.destination_existed_before = destination_item is not None

        if not destination_is_mega and not destination_is_api:
            config_content = source.config_content().rstrip() + "\n" + destination.config_content()
            parent_path = PurePosixPath(item.destination_path).parent.as_posix()
            if not parent_path.startswith("/"):
                parent_path = f"/{parent_path}"
            await self.runner.run(
                "copy",
                [source.remote_path(item.source_path), destination.remote_path(parent_path)],
                config_content,
                record.id,
                should_cancel=self.should_cancel,
                conflict_policy=record.conflict_policy,
            )
            if not await self._verify_rclone_destination(
                destination, item.destination_path, item.size_bytes or 0
            ):
                raise RuntimeError("Destination did not confirm the copied file")
            return "COPIED", "SIZE", None

        async with temporary_staging_area(
            Path(self.settings.transfer_staging_path),
            transfer_id=record.id,
            expected_size=item.size_bytes,
            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
            margin_percent=self.settings.transfer_staging_margin_percent,
        ) as staging_area:
            await self.runner.copy_remote_to_local(
                source.remote_path(item.source_path),
                staging_area,
                source.config_content(),
                record.id,
                should_cancel=self.should_cancel,
            )
            local_file = staging_area / PurePosixPath(item.source_path).name
            if not local_file.is_file() or local_file.stat().st_size != item.size_bytes:
                return "FAILED", None, "SOURCE_SIZE_MISMATCH"
            if destination_is_api:
                stored_path = await destination.upload_file(
                    local_file,
                    item.destination_path,
                    replace=record.conflict_policy != "SKIP",
                    should_cancel=self.should_cancel,
                )
                digest = await _sha256_file(local_file, self.should_cancel)
                if (
                    await destination.sha256(stored_path, should_cancel=self.should_cancel)
                    != digest
                ):
                    return "FAILED", None, "DESTINATION_VERIFICATION_FAILED"
                return "COPIED", "SHA256", None
            if mega_session is None:
                raise RuntimeError("MEGA session is unavailable")
            parent = PurePosixPath(item.destination_path).parent.as_posix()
            if not parent.startswith("/"):
                parent = f"/{parent}"
            await mega_session.upload_file(
                local_file,
                parent,
                should_cancel=self.should_cancel,
            )
            uploaded = await mega_session.get_item(item.destination_path)
            if uploaded is None or uploaded.is_dir or uploaded.size_bytes != item.size_bytes:
                return "FAILED", None, "DESTINATION_VERIFICATION_FAILED"
            return "COPIED", "SIZE", None

    async def _verify_smb_interrupted_copy(
        self,
        source: Any,
        destination: Any,
        item: Any,
        *,
        transfer_id: uuid.UUID,
        destination_is_api: bool,
        mega_session: Any | None,
    ) -> bool:
        """Confirm an interrupted remote write by content before accepting or retrying it."""
        if item.size_bytes is None:
            return False
        expected_staging_bytes = item.size_bytes * (1 if destination_is_api else 2)
        async with temporary_staging_area(
            Path(self.settings.transfer_staging_path),
            transfer_id=transfer_id,
            expected_size=expected_staging_bytes,
            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
            margin_percent=self.settings.transfer_staging_margin_percent,
        ) as staging_area:
            source_area = staging_area / "source"
            source_area.mkdir(mode=0o700)
            await self.runner.copy_remote_to_local(
                source.remote_path(item.source_path),
                source_area,
                source.config_content(),
                transfer_id,
                should_cancel=self.should_cancel,
            )
            source_file = source_area / PurePosixPath(item.source_path).name
            if (
                not source_file.is_file()
                or source_file.is_symlink()
                or source_file.stat().st_size != item.size_bytes
            ):
                return False
            source_digest = await _sha256_file(source_file, self.should_cancel)

            if destination_is_api:
                destination_digest = cast(
                    str,
                    await destination.sha256(
                        item.destination_path, should_cancel=self.should_cancel
                    ),
                )
            else:
                destination_area = staging_area / "destination"
                destination_area.mkdir(mode=0o700)
                if mega_session is not None:
                    destination_file = await mega_session.download_file(
                        item.destination_path,
                        destination_area,
                        should_cancel=self.should_cancel,
                    )
                else:
                    await self.runner.copy_remote_to_local(
                        destination.remote_path(item.destination_path),
                        destination_area,
                        destination.config_content(),
                        transfer_id,
                        should_cancel=self.should_cancel,
                    )
                    destination_file = destination_area / PurePosixPath(item.destination_path).name
                if (
                    not destination_file.is_file()
                    or destination_file.is_symlink()
                    or destination_file.stat().st_size != item.size_bytes
                ):
                    return False
                destination_digest = await _sha256_file(destination_file, self.should_cancel)

            current_source = await source.get_item(item.source_path)
            if (
                current_source is None
                or current_source.is_dir
                or current_source.size_bytes != item.size_bytes
                or not self._same_smb_modified_at(current_source.modified_at, item.modified_at)
            ):
                return False
            return source_digest == destination_digest

    @staticmethod
    def _same_smb_modified_at(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return left is right
        normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
        normalized_right = (
            right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
        )
        return normalized_left == normalized_right

    async def _run_rclone(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
    ) -> None:
        source = self._provider(source_connection)
        destination = self._provider(destination_connection)
        if (source_connection.provider_type, destination_connection.provider_type) == (
            "GOOGLE_DRIVE",
            "NEXTCLOUD",
        ):
            await self._run_drive_nextcloud(record, source, destination)
            return
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
                await self._fail(
                    record,
                    error_code="SOURCE_ITEM_NOT_FOUND",
                    message=(
                        "No se encontró uno de los elementos del origen. Compruebe que "
                        "siga disponible y vuelva a intentarlo."
                    ),
                )
                return
            source_items.append(item)

        try:
            manifest_files, _directories = await self._collect_files(
                source, record.source_paths, transfer_record=record, include_selected_folder=False
            )
        except FileNotFoundError:
            await self._fail(
                record,
                error_code="SOURCE_ITEM_NOT_FOUND",
                message=(
                    "No se encontró uno de los elementos del origen. Compruebe que "
                    "siga disponible y vuelva a intentarlo."
                ),
            )
            return
        except self._PreflightError as exc:
            await self._fail(
                record,
                error_code=exc.error_code,
                message=exc.message,
                technical_details=exc.technical_details,
            )
            return

        selected_roots = [
            (MockStorageProvider.normalize_path(path), item)
            for path, item in zip(record.source_paths, source_items, strict=True)
        ]
        copy_roots = [
            (path, item)
            for index, (path, item) in enumerate(selected_roots)
            if not any(
                other_index != index
                and other_item.is_dir
                and path.startswith(other_path.rstrip("/") + "/")
                for other_index, (other_path, other_item) in enumerate(selected_roots)
            )
            and not any(previous_path == path for previous_path, _item in selected_roots[:index])
        ]
        copy_paths = [path for path, _item in copy_roots]
        copy_items = [item for _path, item in copy_roots]

        record.files_total = len(manifest_files)
        manifest_sizes_known = all(item.size_bytes is not None for item, _path in manifest_files)
        if not manifest_sizes_known:
            unknown_sizes = sum(item.size_bytes is None for item, _path in manifest_files)
            await self._fail(
                record,
                error_code="TRANSFER_FILE_SIZE_UNKNOWN",
                message=(
                    "No se puede validar el tamaño de todos los archivos del origen. "
                    "Consulte al administrador antes de volver a intentarlo."
                ),
                technical_details={"files_without_size": unknown_sizes},
            )
            return

        destination_files: list[tuple[StorageItem, str]] = []
        for item, _relative_path in manifest_files:
            destination_file = self._direct_destination_file_path(
                item.path,
                copy_paths,
                copy_items,
                record.destination_path,
            )
            if destination_file is None:
                await self._fail(
                    record,
                    error_code="TRANSFER_MANIFEST_INVALID",
                    message="No se pudo preparar la lista de archivos para copiar.",
                )
                return
            destination_files.append((item, destination_file))

        record.bytes_total = (
            sum(item.size_bytes or 0 for item, _path in manifest_files)
            if manifest_sizes_known
            else 0
        )

        path_count = max(len(copy_items), 1)
        byte_weights_available = bool(copy_items) and all(
            not item.is_dir and item.size_bytes is not None for item in copy_items
        )
        path_weights = (
            [max(item.size_bytes or 0, 0) for item in copy_items]
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
                record.bytes_transferred = 0
            await self.publish(record)
            byte_offset = 0
            try:
                accumulated_weight = 0
                for path_index, path in enumerate(copy_paths):
                    if await self.should_cancel():
                        await self._cancel(record)
                        return

                    async def on_line(
                        line: str,
                        byte_base: int = byte_offset,
                        path_count_for_job: int = path_count,
                        path_weight_base: int = accumulated_weight,
                        path_weight_for_job: int = path_weights[path_index],
                        total_weight_for_job: int = total_progress_weight,
                        use_byte_weights: bool = byte_weights_available,
                    ) -> None:
                        nonlocal byte_offset
                        progress = parse_rclone_stats_line(line)
                        if progress is None:
                            return
                        if "bytes_transferred" in progress:
                            reported_bytes = progress["bytes_transferred"]
                            if manifest_sizes_known:
                                reported_bytes = min(
                                    reported_bytes,
                                    max(record.bytes_total - byte_base, 0),
                                )
                            record.bytes_transferred = byte_base + reported_bytes
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
                        if manifest_sizes_known and record.bytes_total > 0:
                            # Rclone discovers folder totals incrementally; its first
                            # 100% can describe only the first tiny file it discovered.
                            record.progress_percent = min(
                                round(record.bytes_transferred * 100 / record.bytes_total, 2),
                                99.99,
                            )
                            record.current_item = progress.get("current_item")
                            await self.publish(record)
                            return
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

                    source_item = copy_items[path_index]
                    destination_path = MockStorageProvider.normalize_path(record.destination_path)
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
                    accumulated_weight += path_weights[path_index]
                break
            except RcloneCancelledError:
                raise
            except RcloneCommandError as exc:
                if not exc.retryable or attempt > self.settings.transfer_max_retries:
                    details = exc.diagnostic_details
                    nextcloud_long_name = (
                        "NEXTCLOUD"
                        in {source_connection.provider_type, destination_connection.provider_type}
                        and "FILENAME_TOO_LONG" in details["rclone_error_categories"]
                    )
                    await self._fail(
                        record,
                        error_code=(
                            "NEXTCLOUD_FILENAME_TOO_LONG"
                            if nextcloud_long_name
                            else "RCLONE_RETRIES_EXHAUSTED"
                            if exc.retryable
                            else "RCLONE_COMMAND_FAILED"
                        ),
                        message=(
                            "Nextcloud rechazó un archivo porque su nombre es demasiado largo. "
                            "Acorte el nombre y vuelva a intentar la copia."
                            if nextcloud_long_name
                            else "No se pudo completar la copia con el proveedor."
                        ),
                        technical_details={
                            "return_code": exc.return_code,
                            "retryable": exc.retryable,
                            "attempt": attempt,
                            **details,
                        },
                    )
                    return
                record.status = "RETRYING"
                await self.publish(record)
                delay = min(5 * (2 ** (attempt - 1)) + random.random(), 300)
                if not await self._wait_for_retry(record, delay, next_attempt=attempt + 1):
                    return
        else:
            await self._fail(record)
            return

        record.current_item = None
        record.current_speed_bps = None
        record.estimated_remaining_seconds = None
        await self.publish(record)
        skipped_bytes = 0
        for item, destination_file in destination_files:
            record.current_item = item.path
            assert item.size_bytes is not None
            destination_item = await self._read_rclone_destination(
                destination, destination_file, item.size_bytes
            )
            if destination_item is None or destination_item.is_dir:
                await self._fail(
                    record,
                    error_code="DESTINATION_VERIFICATION_FAILED",
                    message=(
                        f"El proveedor no confirmó el archivo «{item.name}». Revise el "
                        "destino antes de volver a intentarlo."
                    ),
                    technical_details={
                        "file_name": item.name,
                        "expected_bytes": item.size_bytes,
                    },
                )
                return
            if destination_item.size_bytes != item.size_bytes:
                if record.conflict_policy == "SKIP":
                    record.warning_count += 1
                    skipped_bytes += item.size_bytes or 0
                else:
                    await self._fail(
                        record,
                        error_code="DESTINATION_VERIFICATION_FAILED",
                        message=(
                            f"El proveedor no confirmó el archivo «{item.name}». Revise el "
                            "destino antes de volver a intentarlo."
                        ),
                        technical_details={
                            "file_name": item.name,
                            "expected_bytes": item.size_bytes,
                            "destination_bytes": destination_item.size_bytes,
                        },
                    )
                    return
            record.files_completed += 1
            record.progress_percent = max(
                record.progress_percent,
                min(
                    round(record.files_completed * 100 / max(record.files_total, 1), 2),
                    99.99,
                ),
            )
            await self.publish(record)

        record.current_item = None
        record.current_speed_bps = None
        record.files_completed = record.files_total
        if manifest_sizes_known:
            record.bytes_transferred = max(record.bytes_total - skipped_bytes, 0)
        record.progress_percent = 100.0
        record.status = "COMPLETED_WITH_WARNINGS" if record.warning_count else "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _run_drive_nextcloud(
        self, record: TransferRecord, source: Any, destination: Any
    ) -> None:
        """Verify each file; export unknown-size Drive documents exactly once to staging."""
        export_limit = min(self.settings.transfer_max_file_bytes or 64 * 1024**2, 64 * 1024**2)
        try:
            files, directories = await self._collect_files(
                source,
                record.source_paths,
                transfer_record=record,
                include_selected_folder=True,
                allow_unknown_size=True,
            )
            for directory in directories:
                if any(len(part.encode("utf-8")) > 255 for part in PurePosixPath(directory).parts):
                    raise self._PreflightError(
                        "NEXTCLOUD_FILENAME_TOO_LONG",
                        "Nextcloud no admite uno de los nombres de carpeta. Acorte el nombre.",
                    )
            for item, relative in files:
                record.current_item = item.path
                if any(len(part.encode("utf-8")) > 255 for part in PurePosixPath(relative).parts):
                    raise self._PreflightError(
                        "NEXTCLOUD_FILENAME_TOO_LONG",
                        "Nextcloud no admite uno de los nombres de la selección. "
                        "Acorte el nombre del archivo o carpeta antes de copiar.",
                        {
                            "name_utf8_bytes": max(
                                len(p.encode("utf-8")) for p in PurePosixPath(relative).parts
                            )
                        },
                    )
            exports = [
                (item.model_copy(update={"size_bytes": export_limit * 2}), path)
                for item, path in files
                if item.size_bytes is None
            ]
            if exports and not await self._preflight_staging(record, exports):
                return
        except (FileNotFoundError, self._PreflightError) as exc:
            await self._fail(
                record,
                error_code=exc.error_code
                if isinstance(exc, self._PreflightError)
                else "SOURCE_ITEM_NOT_FOUND",
                message=exc.message
                if isinstance(exc, self._PreflightError)
                else "No se encontró un archivo del origen.",
                technical_details=exc.technical_details
                if isinstance(exc, self._PreflightError)
                else None,
            )
            return

        unknown_sizes = any(item.size_bytes is None for item, _ in files)
        record.files_total = len(files)
        record.files_completed = 0
        record.bytes_transferred = 0
        record.bytes_total = 0 if unknown_sizes else sum(item.size_bytes or 0 for item, _ in files)
        record.current_item = None
        record.status = "RUNNING"
        await self.publish(record)
        destination_root = MockStorageProvider.normalize_path(record.destination_path)
        empty_dirs = {
            directory
            for directory in directories
            if not any(path.startswith(directory.rstrip("/") + "/") for _, path in files)
        }
        if empty_dirs:
            async with temporary_staging_area(
                Path(self.settings.transfer_staging_path), transfer_id=record.id, expected_size=0
            ) as area:
                for directory in empty_dirs:
                    area.joinpath(*PurePosixPath(self._safe_relative_path(directory)).parts).mkdir(
                        parents=True, exist_ok=True
                    )
                await self.runner.copy_local_directory_to_remote(
                    area,
                    destination.remote_path(destination_root),
                    destination.config_content(),
                    record.id,
                    conflict_policy=record.conflict_policy,
                    should_cancel=self.should_cancel,
                )

        resolved_bytes = 0
        for item, relative in files:
            if await self.should_cancel():
                raise RcloneCancelledError("Transfer cancelled")
            target = MockStorageProvider.normalize_path(
                f"{destination_root.rstrip('/')}/{self._safe_relative_path(relative)}"
            )
            folder = str(PurePosixPath(target).parent)
            record.current_item = item.path
            record.current_speed_bps = None
            record.estimated_remaining_seconds = None
            byte_base = record.bytes_transferred

            async def on_progress(
                line: str,
                item_for_file: StorageItem = item,
                byte_base_for_file: int = byte_base,
                resolved_base: int = resolved_bytes,
            ) -> None:
                metrics = parse_rclone_stats_line(line)
                if metrics is None:
                    return
                done = min(
                    max(metrics.get("bytes_transferred", 0), 0), item_for_file.size_bytes or 0
                )
                record.bytes_transferred = byte_base_for_file + done
                record.current_speed_bps = metrics.get("current_speed_bps")
                elapsed = (
                    max((datetime.now(UTC) - record.started_at).total_seconds(), 1)
                    if record.started_at
                    else 1
                )
                record.average_speed_bps = int(record.bytes_transferred / elapsed)
                fraction = done / item_for_file.size_bytes if item_for_file.size_bytes else 0
                percent = (
                    (record.files_completed + fraction) * 100 / max(record.files_total, 1)
                    if unknown_sizes or not record.bytes_total
                    else (resolved_base + done) * 100 / record.bytes_total
                )
                record.progress_percent = min(round(percent, 2), 99.99)
                await self.publish(record)

            existing = await destination.get_item(target)
            if existing is not None and existing.is_dir:
                await self._fail(record, error_code="DESTINATION_PATH_IS_DIRECTORY")
                return
            if existing is not None and record.conflict_policy == "SKIP":
                # Preservation is explicit; unknown exports cannot be certified by size.
                if item.size_bytes is None or existing.size_bytes != item.size_bytes:
                    record.warning_count += 1
            else:
                try:
                    if item.size_bytes is None:
                        async with temporary_staging_area(
                            Path(self.settings.transfer_staging_path),
                            transfer_id=record.id,
                            expected_size=export_limit * 2,
                            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
                            margin_percent=self.settings.transfer_staging_margin_percent,
                        ) as area:

                            async def download_export(
                                source_item: StorageItem = item, export_area: Path = area
                            ) -> bool:
                                # A failed export may leave a partial local file.
                                (export_area / source_item.name).unlink(missing_ok=True)
                                await self.runner.copy_remote_to_local(
                                    source.remote_path(source_item.path),
                                    export_area,
                                    source.config_content(),
                                    record.id,
                                    should_cancel=self.should_cancel,
                                    max_transfer_bytes=export_limit,
                                )
                                return True

                            await self._copy_drive_file_with_retry(record, download_export)
                            local = area / item.name
                            if not local.is_file() or local.is_symlink():
                                raise self._PreflightError(
                                    "GOOGLE_DRIVE_EXPORT_FAILED",
                                    "Drive no confirmó el archivo exportado.",
                                )
                            actual_size = local.stat().st_size
                            if actual_size > export_limit:
                                raise self._PreflightError(
                                    "GOOGLE_DRIVE_EXPORT_LIMIT_EXCEEDED",
                                    "La exportación supera el límite temporal permitido.",
                                )
                            item.size_bytes = actual_size
                            if all(i.size_bytes is not None for i, _ in files):
                                record.bytes_total = sum(i.size_bytes or 0 for i, _ in files)
                            digest = await _sha256_file(local, self.should_cancel)

                            async def copy_export(
                                target_path: str = target,
                                file_digest: str = digest,
                                file_size: int = actual_size,
                                local_file: Path = local,
                                remote_folder: str = folder,
                                progress_callback: Callable[[str], Awaitable[None]] = on_progress,
                            ) -> bool:
                                if await destination.get_item(
                                    target_path
                                ) is not None and await self._verify_drive_export(
                                    destination, target_path, file_digest, file_size
                                ):
                                    return True
                                await self.runner.copy_local_to_remote(
                                    local_file,
                                    destination.remote_path(remote_folder),
                                    destination.config_content(),
                                    record.id,
                                    should_cancel=self.should_cancel,
                                    line_callback=progress_callback,
                                    conflict_policy=record.conflict_policy,
                                    force_transfer=True,
                                )
                                return await self._verify_drive_export(
                                    destination, target_path, file_digest, file_size
                                )

                            verified = await self._copy_drive_file_with_retry(record, copy_export)
                    else:

                        async def copy_binary(
                            source_item: StorageItem = item,
                            remote_folder: str = folder,
                            target_path: str = target,
                            progress_callback: Callable[[str], Awaitable[None]] = on_progress,
                        ) -> bool:
                            await self.runner.run(
                                "copy",
                                [
                                    source.remote_path(source_item.path),
                                    destination.remote_path(remote_folder),
                                ],
                                source.config_content().rstrip()
                                + "\n"
                                + destination.config_content(),
                                record.id,
                                should_cancel=self.should_cancel,
                                line_callback=progress_callback,
                                conflict_policy=record.conflict_policy,
                            )
                            assert source_item.size_bytes is not None
                            return await self._verify_rclone_destination(
                                destination, target_path, source_item.size_bytes
                            )

                        verified = await self._copy_drive_file_with_retry(record, copy_binary)
                    if not verified:
                        await self._fail(
                            record,
                            error_code="DESTINATION_VERIFICATION_FAILED",
                            message="Nextcloud no confirmó el contenido del archivo copiado.",
                        )
                        return
                except self._PreflightError as exc:
                    await self._fail(record, error_code=exc.error_code, message=exc.message)
                    return
                except StagingSpaceError:
                    await self._fail(record, error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT")
                    return
                except RcloneCommandError as exc:
                    details = exc.diagnostic_details
                    long_name = "FILENAME_TOO_LONG" in details["rclone_error_categories"]
                    await self._fail(
                        record,
                        error_code="NEXTCLOUD_FILENAME_TOO_LONG"
                        if long_name
                        else "GOOGLE_DRIVE_EXPORT_LIMIT_EXCEEDED"
                        if exc.return_code == 8
                        else "RCLONE_RETRIES_EXHAUSTED"
                        if exc.retryable
                        else "RCLONE_COMMAND_FAILED",
                        message="Nextcloud rechazó un nombre demasiado largo."
                        if long_name
                        else "No se pudo completar la copia con el proveedor.",
                        technical_details={
                            "return_code": exc.return_code,
                            "retryable": exc.retryable,
                            **details,
                        },
                    )
                    return
            resolved_bytes += item.size_bytes or 0
            record.bytes_transferred = byte_base + (item.size_bytes or 0)
            record.files_completed += 1
            record.progress_percent = min(
                round(
                    record.files_completed * 100 / max(record.files_total, 1)
                    if unknown_sizes or not record.bytes_total
                    else resolved_bytes * 100 / record.bytes_total,
                    2,
                ),
                99.99,
            )
            record.current_speed_bps = None
            await self.publish(record)

        record.current_item = None
        record.current_speed_bps = None
        record.estimated_remaining_seconds = None
        record.progress_percent = 100
        record.status = "COMPLETED_WITH_WARNINGS" if record.warning_count else "COMPLETED"
        record.completed_at = datetime.now(UTC)
        await self.publish(record)

    async def _copy_drive_file_with_retry(
        self, record: TransferRecord, operation: Callable[[], Awaitable[bool]]
    ) -> bool:
        for attempt in range(1, self.settings.transfer_max_retries + 2):
            if await self.should_cancel():
                raise RcloneCancelledError("Transfer cancelled")
            record.attempt_count = max(record.attempt_count, attempt)
            try:
                return await operation()
            except RcloneCommandError as exc:
                if not exc.retryable or attempt > self.settings.transfer_max_retries:
                    raise
                record.status = "RETRYING"
                record.error_count += 1
                await self.publish(record)
                if not await self._wait_for_retry(
                    record, min(5 * 2 ** (attempt - 1), 300), next_attempt=attempt + 1
                ):
                    raise RcloneCancelledError("Transfer cancelled") from None
                record.status = "RUNNING"
        return False

    async def _verify_drive_export(
        self, destination: Any, path: str, digest: str, size: int
    ) -> bool:
        if not await self._verify_rclone_destination(destination, path, size):
            return False
        async with temporary_staging_area(
            Path(self.settings.transfer_staging_path),
            transfer_id=uuid.uuid4(),
            expected_size=size,
            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
        ) as area:
            await self.runner.copy_remote_to_local(
                destination.remote_path(path),
                area,
                destination.config_content(),
                uuid.uuid4(),
                should_cancel=self.should_cancel,
                max_transfer_bytes=size + 1,
            )
            downloaded = area / PurePosixPath(path).name
            return (
                downloaded.is_file()
                and not downloaded.is_symlink()
                and downloaded.stat().st_size == size
                and await _sha256_file(downloaded, self.should_cancel) == digest
            )

    @staticmethod
    def _uses_seafile_api(connection: ConnectionRecord) -> bool:
        return connection.provider_type == "SEAFILE" and connection.adapter_type == "SEAFILE_API"

    @staticmethod
    def _versioned_identity(item: StorageItem) -> str:
        return f"object:{item.object_id}" if item.object_id else f"path:{item.path}"

    async def _collect_versioned_tree(
        self,
        source: Any,
        record: TransferRecord,
        *,
        allow_unknown_size: bool,
        maximum_files: int,
    ) -> tuple[list[tuple[StorageItem, str]], list[tuple[StorageItem, str]]]:
        async def no_progress(_progress: InventoryProgress) -> None:
            return None

        try:
            inventory = await SourceInventoryScanner(self.settings).scan(
                source,
                record,
                allow_unknown_size=allow_unknown_size,
                maximum_files=maximum_files,
                on_progress=no_progress,
                should_cancel=self.should_cancel,
            )
        except InventoryPreflightError as exc:
            raise self._PreflightError(
                exc.error_code,
                exc.message,
                exc.technical_details,
            ) from exc
        return inventory.files, inventory.directories

    async def _list_versioned_destination(self, provider: Any, path: str) -> list[StorageItem]:
        items: list[StorageItem] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            options: dict[str, Any] = {}
            if isinstance(provider, RcloneStorageProvider):
                options["force_refresh"] = cursor is None
            page = await provider.list_items(path, cursor=cursor, page_size=200, **options)
            items.extend(page.items)
            if page.next_cursor is None:
                return items
            if page.next_cursor in seen_cursors:
                raise ValueError("Destination returned a repeated page cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    @staticmethod
    def _manifest_identity(item: TransferManifestItemRecord) -> str:
        if item.source_object_id:
            return f"object:{item.source_object_id}"
        return f"path:{item.source_path}"

    async def _iterate_versioned_manifest_rows(
        self,
        transfer_id: uuid.UUID,
        *,
        statuses: set[str],
    ) -> AsyncIterator[TransferManifestItemRecord]:
        cursor_id: uuid.UUID | None = None
        while True:
            page = await self.store.alist_transfer_manifest_items(
                transfer_id,
                statuses=statuses,
                after_id=cursor_id,
                limit=200,
                include_directories=True,
            )
            if not page:
                return
            for item in page:
                yield item
            cursor_id = page[-1].id

    @staticmethod
    def _remote_join(parent: str, name: str) -> str:
        return MockStorageProvider.normalize_path(f"{parent.rstrip('/')}/{name}")

    @staticmethod
    def _versioning_reason(
        requested_path: str, assigned_path: str, original_name: str
    ) -> str | None:
        if requested_path == assigned_path:
            return None
        if len(original_name.encode("utf-8")) > 255:
            return "COMPONENT_LIMIT"
        return "NAME_CONFLICT"

    async def _run_versioned_copy(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
    ) -> None:
        """Copy each inventoried item to a durable, numbered destination assignment."""
        if destination_connection.provider_type == "SMB" or (
            source_connection.provider_type == "SMB"
            and destination_connection.provider_type
            not in {"GOOGLE_DRIVE", "MEGA", "SEAFILE", "NEXTCLOUD"}
        ):
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return
        if source_connection.provider_type == destination_connection.provider_type == "MEGA":
            await self._fail(record, error_code="PROVIDER_PAIR_UNSUPPORTED")
            return
        source = self._provider(source_connection)
        destination = self._provider(destination_connection)
        async with AsyncExitStack() as stack:
            if source_connection.provider_type == "MEGA":
                source = await stack.enter_async_context(
                    source.open_session(transfer_id=record.id, should_cancel=self.should_cancel)
                )
            if destination_connection.provider_type == "MEGA":
                destination = await stack.enter_async_context(
                    destination.open_session(
                        transfer_id=record.id,
                        should_cancel=self.should_cancel,
                    )
                )
            try:
                files, directories = await self._collect_versioned_tree(
                    source,
                    record,
                    allow_unknown_size=source_connection.provider_type == "GOOGLE_DRIVE",
                    maximum_files=(
                        self.settings.smb_transfer_max_files
                        if source_connection.provider_type == "SMB"
                        else self.settings.transfer_max_files
                    ),
                )
            except FileNotFoundError:
                prior_counts = await self.store.aget_transfer_manifest_counts(record.id)
                incompatible_manifest = (
                    source_connection.provider_type == "MEGA" and prior_counts.get("total", 0) > 0
                )
                await self._fail(
                    record,
                    error_code=(
                        "MEGA_MANIFEST_REBUILD_REQUIRED"
                        if incompatible_manifest
                        else "SOURCE_ITEM_NOT_FOUND"
                    ),
                    message=(
                        "El origen difiere del manifiesto MEGA; cree una nueva transferencia."
                        if incompatible_manifest
                        else "No se encontró el archivo de origen."
                    ),
                )
                return
            except self._PreflightError as exc:
                await self._fail(
                    record,
                    error_code=exc.error_code,
                    message=exc.message,
                    technical_details=exc.technical_details,
                )
                return

            unknown_size_files = [(item, path) for item, path in files if item.size_bytes is None]
            if unknown_size_files and source_connection.provider_type != "GOOGLE_DRIVE":
                await self._fail(record, error_code="TRANSFER_FILE_SIZE_UNKNOWN")
                return
            known_files = [(item, path) for item, path in files if item.size_bytes is not None]
            if known_files and not await self._preflight_staging(record, known_files):
                return
            if not files and not directories:
                record.status = "RUNNING"
                record.files_total = 0
                record.progress_percent = 100.0
                record.status = "COMPLETED"
                record.completed_at = datetime.now(UTC)
                await self.publish(record)
                return

            destination_root = MockStorageProvider.normalize_path(record.destination_path)
            manifest_statuses = {"DIRECTORY", "PENDING", "COPIED", "SKIPPED", "FAILED"}
            previous_rows = [
                row
                async for row in self._iterate_versioned_manifest_rows(
                    record.id, statuses=manifest_statuses
                )
            ]
            previous = {(row.is_dir, self._manifest_identity(row)): row for row in previous_rows}
            if source_connection.provider_type == "MEGA" and previous_rows:
                current_paths = {
                    (item.is_dir, self._versioned_identity(item)): item.path
                    for item, _ in [*directories, *files]
                }
                if any(
                    current_paths.get((row.is_dir, self._manifest_identity(row))) != row.source_path
                    for row in previous_rows
                ):
                    await self._fail(
                        record,
                        error_code="MEGA_MANIFEST_REBUILD_REQUIRED",
                        message=(
                            "El inventario MEGA difiere del origen; cree una nueva transferencia."
                        ),
                    )
                    return
            assigned_directories: dict[str, str] = {}
            relative_directories: dict[str, str] = {".": destination_root}
            occupied_by_parent: dict[str, set[str]] = {}
            cached_children: dict[str, list[StorageItem]] = {}
            planned_new_directories: set[str] = set()
            manifest_entries: list[dict[str, Any]] = []

            async def destination_children(parent_path: str) -> list[StorageItem]:
                if parent_path not in cached_children:
                    cached_children[parent_path] = (
                        []
                        if parent_path in planned_new_directories
                        else await self._list_versioned_destination(destination, parent_path)
                    )
                    occupied_by_parent[parent_path] = {
                        collision_key(item.name) for item in cached_children[parent_path]
                    }
                return cached_children[parent_path]

            def parent_destination(item: StorageItem, relative_path: str) -> str:
                relative_parent = PurePosixPath(relative_path).parent.as_posix()
                if relative_parent == ".":
                    return destination_root
                if item.parent_object_id and item.parent_object_id in assigned_directories:
                    return assigned_directories[item.parent_object_id]
                return relative_directories.get(relative_parent, destination_root)

            for directory, relative_path in sorted(
                directories,
                key=lambda entry: (len(PurePosixPath(entry[1]).parts), entry[1]),
            ):
                identity = self._versioned_identity(directory)
                stored = previous.get((True, identity))
                raw_relative = PurePosixPath(relative_path)
                parent_path = parent_destination(directory, relative_path)
                requested_path = self._remote_join(
                    destination_root,
                    raw_relative.as_posix(),
                )
                if stored is not None:
                    assigned_path = stored.destination_path
                    rename_reason = stored.rename_reason
                else:
                    children = await destination_children(parent_path)
                    occupied = occupied_by_parent.setdefault(parent_path, set())
                    same_name = [
                        child
                        for child in children
                        if collision_key(child.name) == collision_key(directory.name)
                    ]
                    same_named_directory = (
                        same_name[0] if len(same_name) == 1 and same_name[0].is_dir else None
                    )
                    if same_named_directory is not None and collision_key(
                        directory.name
                    ) not in reserved_directory_names(parent_path, manifest_entries):
                        assigned_path = same_named_directory.path
                        actual_name = same_named_directory.name
                    else:
                        occupied_names = {child.name for child in children}
                        occupied_names.update(
                            entry["destination_path"].rsplit("/", 1)[-1]
                            for entry in manifest_entries
                            if entry.get("is_dir")
                            and entry["destination_path"].rsplit("/", 1)[0]
                            == parent_path.rstrip("/")
                        )
                        actual_name = allocate_available_name(directory.name, occupied_names, 255)
                        assigned_path = self._remote_join(parent_path, actual_name)
                        planned_new_directories.add(assigned_path)
                    occupied.add(collision_key(actual_name))
                    rename_reason = self._versioning_reason(
                        requested_path, assigned_path, directory.name
                    )
                occupied_by_parent.setdefault(parent_path, set()).add(
                    collision_key(PurePosixPath(assigned_path).name)
                )
                assigned_directories[directory.object_id or identity] = assigned_path
                relative_directories.setdefault(relative_path, assigned_path)
                manifest_entries.append(
                    {
                        "source_path": directory.path,
                        "source_object_id": directory.object_id,
                        "destination_path": assigned_path,
                        "requested_destination_path": requested_path,
                        "rename_reason": rename_reason,
                        "is_dir": True,
                        "size_bytes": None,
                        "modified_at": directory.modified_at,
                    }
                )

            for item, relative_path in files:
                identity = self._versioned_identity(item)
                stored = previous.get((False, identity))
                relative = PurePosixPath(relative_path)
                parent_path = parent_destination(item, relative_path)
                requested_path = self._remote_join(destination_root, relative.as_posix())
                if stored is not None:
                    assigned_path = stored.destination_path
                    rename_reason = stored.rename_reason
                else:
                    children = await destination_children(parent_path)
                    occupied = occupied_by_parent.setdefault(
                        parent_path,
                        {collision_key(child.name) for child in children},
                    )
                    name = allocate_available_name(
                        item.name,
                        {child.name for child in children}
                        | {
                            entry["destination_path"].rsplit("/", 1)[-1]
                            for entry in manifest_entries
                            if entry["destination_path"].rsplit("/", 1)[0]
                            == parent_path.rstrip("/")
                        },
                        255,
                    )
                    assigned_path = self._remote_join(parent_path, name)
                    rename_reason = self._versioning_reason(
                        requested_path, assigned_path, item.name
                    )
                    occupied.add(collision_key(name))
                manifest_entries.append(
                    {
                        "source_path": item.path,
                        "source_object_id": item.object_id,
                        "destination_path": assigned_path,
                        "requested_destination_path": requested_path,
                        "rename_reason": rename_reason,
                        "is_dir": False,
                        "size_bytes": item.size_bytes,
                        "modified_at": item.modified_at,
                    }
                )

            await self.store.aadd_transfer_manifest_items(record.id, manifest_entries)
            rows = [
                row
                async for row in self._iterate_versioned_manifest_rows(
                    record.id, statuses=manifest_statuses
                )
            ]
            row_by_identity = {(row.is_dir, self._manifest_identity(row)): row for row in rows}
            expected_identities = {
                (True, self._versioned_identity(item)) for item, _relative in directories
            } | {(False, self._versioned_identity(item)) for item, _relative in files}
            missing_identities = expected_identities - row_by_identity.keys()
            if missing_identities:
                await self._fail(
                    record,
                    error_code="MANIFEST_INCOMPLETE",
                    message="No se pudo confirmar el manifiesto completo antes de copiar.",
                    technical_details={
                        "expected_items": len(expected_identities),
                        "persisted_items": len(row_by_identity),
                        "missing_items": len(missing_identities),
                    },
                )
                return
            for directory_row in (row for row in rows if row.is_dir):
                if not directory_row.scan_complete:
                    await self.store.amark_transfer_directory_scanned(record.id, directory_row.id)
            directory_count = sum(1 for row in rows if row.is_dir and row.rename_reason is not None)
            renamed_file_count = sum(
                1 for row in rows if not row.is_dir and row.rename_reason is not None
            )
            existing_file_warnings = sum(
                1 for row in rows if not row.is_dir and row.status in {"FAILED", "SKIPPED"}
            )
            record.files_total = len(files)
            record.bytes_total = sum(item.size_bytes or 0 for item, _relative in files)
            record.warning_count = directory_count + renamed_file_count + existing_file_warnings
            record.status = "RUNNING"
            await self.publish(record)

            ordered_rows = [
                row_by_identity[(True, self._versioned_identity(item))]
                for item, _relative in sorted(
                    directories,
                    key=lambda entry: (len(PurePosixPath(entry[1]).parts), entry[1]),
                )
            ]
            if destination_connection.provider_type == "MEGA":
                for row in ordered_rows:
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    await destination.mkdir(row.destination_path)
            elif self._uses_seafile_api(destination_connection):
                for row in ordered_rows:
                    if await self.should_cancel():
                        await self._cancel(record)
                        return
                    await destination.mkdir(row.destination_path, should_cancel=self.should_cancel)
            elif ordered_rows:
                async with temporary_staging_area(
                    Path(self.settings.transfer_staging_path),
                    transfer_id=record.id,
                    expected_size=0,
                ) as area:
                    for row in ordered_rows:
                        relative_path = self._safe_relative_path(
                            row.destination_path[len(destination_root.rstrip("/")) :].lstrip("/")
                        )
                        area.joinpath(*PurePosixPath(relative_path).parts).mkdir(
                            parents=True, exist_ok=True
                        )
                    await self.runner.copy_local_directory_to_remote(
                        area,
                        destination.remote_path(destination_root),
                        destination.config_content(),
                        record.id,
                        should_cancel=self.should_cancel,
                        conflict_policy="KEEP_BOTH",
                    )

            last_failure: tuple[str | None, str | None] | None = None
            consecutive_failures = 0
            for item, _relative_path in files:
                row = row_by_identity[(False, self._versioned_identity(item))]
                if row.omission_kind is not None:
                    continue
                if row.status == "COPIED":
                    continue
                await self._checkpoint_file_boundary(record, row.source_path)
                if await self.should_cancel():
                    await self._cancel(record)
                    return
                current_path = row.destination_path
                if row.copy_started:
                    existing = await self._find_versioned_destination_item(
                        destination, current_path
                    )
                    if existing is None:
                        await self.store.aupdate_transfer_manifest_item(
                            record.id,
                            row.id,
                            status="FAILED",
                            error_code="DESTINATION_STATE_UNCERTAIN",
                        )
                        await self._refresh_versioned_record(record)
                        await self._fail(
                            record,
                            error_code="DESTINATION_STATE_UNCERTAIN",
                            message="La copia interrumpida no puede reanudarse con seguridad.",
                        )
                        return
                    # An interrupted write may have completed remotely. Reconcile by content
                    # before accepting it; otherwise stop without changing the assigned name.
                    if not await self._reconcile_versioned_destination(
                        record,
                        source_connection,
                        destination_connection,
                        source,
                        destination,
                        item,
                        existing,
                        current_path,
                    ):
                        await self.store.aupdate_transfer_manifest_item(
                            record.id,
                            row.id,
                            status="FAILED",
                            error_code="DESTINATION_STATE_UNCERTAIN",
                        )
                        await self._refresh_versioned_record(record)
                        await self._fail(
                            record,
                            error_code="DESTINATION_STATE_UNCERTAIN",
                            message="La copia interrumpida no pudo verificarse por contenido.",
                        )
                        return
                    await self.store.aupdate_transfer_manifest_item(
                        record.id, row.id, status="COPIED", verified_by="SHA256_RECONCILED"
                    )
                    await self._refresh_versioned_record(record)
                    last_failure, consecutive_failures = None, 0
                    continue

                result = await self._copy_versioned_file(
                    record,
                    source_connection,
                    destination_connection,
                    source,
                    destination,
                    item,
                    row,
                )
                if result.outcome == CopyOutcome.STOPPED:
                    return
                if result.outcome == CopyOutcome.SKIPPED:
                    continue
                if result.outcome == CopyOutcome.FAILED_SAFE:
                    failure = (result.error_code, result.provider_stage)
                    consecutive_failures = (
                        consecutive_failures + 1 if failure == last_failure else 1
                    )
                    last_failure = failure
                    if consecutive_failures >= 3:
                        await self._fail(
                            record,
                            error_code="TRANSFER_REPEATED_FILE_FAILURE",
                            message="Tres archivos consecutivos fallaron en la misma etapa.",
                            technical_details={
                                "provider_stage": result.provider_stage,
                                "file_error_code": result.error_code,
                            },
                            count_error=False,
                        )
                        return
                else:
                    last_failure, consecutive_failures = None, 0

            counts = await self.store.aget_transfer_manifest_counts(record.id)
            record = await self.store.aget_transfer(record.id) or record
            record.current_item = None
            record.current_speed_bps = None
            record.warning_count = (
                counts.get("failed", counts.get("FAILED", 0))
                + counts.get("skipped", counts.get("SKIPPED", 0))
                + directory_count
                + renamed_file_count
            )
            record.error_count = counts.get("failed", 0)
            record.progress_percent = smb_manifest_progress_percent(
                bytes_transferred=record.bytes_transferred,
                bytes_total=record.bytes_total,
                files_total=record.files_total,
                counts=counts,
            )
            if counts.get("failed", 0) or counts.get("pending", 0):
                await self._fail(
                    record,
                    error_code="TRANSFER_FILES_INCOMPLETE",
                    message="La transferencia terminó con archivos fallidos o pendientes.",
                    technical_details={"manifest_summary": counts},
                    count_error=False,
                )
                return
            record.status = "COMPLETED_WITH_WARNINGS" if record.warning_count else "COMPLETED"
            record.completed_at = datetime.now(UTC)
            await self.publish(record)

    async def _find_versioned_destination_item(
        self, destination: Any, destination_path: str
    ) -> StorageItem | None:
        parent = PurePosixPath(destination_path).parent.as_posix()
        name = PurePosixPath(destination_path).name
        items = await self._list_versioned_destination(destination, parent)
        return next(
            (item for item in items if collision_key(item.name) == collision_key(name)),
            None,
        )

    async def _download_versioned_source(
        self,
        record: TransferRecord,
        connection: ConnectionRecord,
        source: Any,
        item: StorageItem,
        area: Path,
        *,
        max_bytes: int | None,
    ) -> Path:
        target = area / item.name
        if connection.provider_type == "MEGA":
            return cast(
                Path,
                await source.download_file(
                    item.path,
                    area,
                    object_id=item.object_id,
                    should_cancel=self.should_cancel,
                ),
            )
        if (
            connection.provider_type == "GOOGLE_DRIVE"
            and isinstance(source, RcloneGoogleDriveProvider)
            and item.object_id is not None
        ):
            await source.download_file_by_id(
                item,
                target,
                should_cancel=self.should_cancel,
                max_bytes=max_bytes,
            )
            return target
        if self._uses_seafile_api(connection):
            await source.download_file(
                item.path,
                target,
                should_cancel=self.should_cancel,
            )
            return target
        await self.runner.copy_remote_to_local(
            source.remote_path(item.path),
            area,
            source.config_content(),
            record.id,
            should_cancel=self.should_cancel,
            conflict_policy="KEEP_BOTH",
            max_transfer_bytes=max_bytes,
        )
        return target

    async def _confirmed_source_item_missing(
        self, source: Any, item: StorageItem, error: Exception
    ) -> bool:
        try:
            if isinstance(error, GoogleDriveApiError):
                if error.status_code != 404 or not item.object_id:
                    return False
                return (
                    await source.get_item(
                        item.path,
                        object_id=item.object_id,
                        parent_object_id=item.parent_object_id,
                    )
                    is None
                )
            if isinstance(error, SeafileApiError):
                if error.status_code != 404:
                    return False
                return await source.get_item(item.path) is None
            if not isinstance(error, RcloneCommandError) or not error.source_item_missing:
                return False

            parent_path = PurePosixPath(item.path).parent.as_posix()
            cursor: str | None = None
            while True:
                page = await source.list_items(
                    parent_path,
                    cursor=cursor,
                    page_size=200,
                    force_refresh=True,
                )
                if any(
                    candidate.path == item.path
                    and (item.object_id is None or candidate.object_id == item.object_id)
                    for candidate in page.items
                ):
                    return False
                if page.next_cursor is None:
                    return True
                cursor = page.next_cursor
        except Exception:
            # A failed or inconclusive metadata check is systemic/ambiguous, so stop.
            return False

    async def _upload_versioned_destination(
        self,
        record: TransferRecord,
        connection: ConnectionRecord,
        destination: Any,
        local_file: Path,
        destination_path: str,
    ) -> str | None:
        parent = PurePosixPath(destination_path).parent.as_posix()
        if connection.provider_type == "MEGA":
            await destination.upload_file(
                local_file,
                parent,
                should_cancel=self.should_cancel,
            )
            return destination_path
        if self._uses_seafile_api(connection):
            return cast(
                str | None,
                await destination.upload_file(
                    local_file,
                    destination_path,
                    replace=False,
                    should_cancel=self.should_cancel,
                ),
            )
        await self.runner.copy_local_to_remote(
            local_file,
            destination.remote_path(parent),
            destination.config_content(),
            record.id,
            should_cancel=self.should_cancel,
            conflict_policy="KEEP_BOTH",
        )
        return destination_path

    async def _hash_versioned_destination(
        self,
        record: TransferRecord,
        connection: ConnectionRecord,
        destination: Any,
        item: StorageItem,
        destination_path: str,
        area: Path,
    ) -> str:
        if self._uses_seafile_api(connection):
            return cast(
                str, await destination.sha256(destination_path, should_cancel=self.should_cancel)
            )
        verify_area = area / "destination-verification"
        verify_area.mkdir(mode=0o700)
        if connection.provider_type == "MEGA":
            verified_file = await destination.download_file(
                destination_path,
                verify_area,
                object_id=item.object_id,
                should_cancel=self.should_cancel,
            )
        elif (
            connection.provider_type == "GOOGLE_DRIVE"
            and isinstance(destination, RcloneGoogleDriveProvider)
            and item.object_id is not None
        ):
            verified_file = verify_area / item.name
            await destination.download_file_by_id(
                item,
                verified_file,
                should_cancel=self.should_cancel,
                max_bytes=self.settings.transfer_max_file_bytes or None,
            )
        else:
            await self.runner.copy_remote_to_local(
                destination.remote_path(destination_path),
                verify_area,
                destination.config_content(),
                record.id,
                should_cancel=self.should_cancel,
                conflict_policy="KEEP_BOTH",
                max_transfer_bytes=self.settings.transfer_max_file_bytes or None,
            )
            verified_file = verify_area / item.name
        if not verified_file.is_file() or verified_file.is_symlink():
            raise RuntimeError("Destination verification did not produce a regular file")
        return await _sha256_file(verified_file, self.should_cancel)

    async def _reconcile_versioned_destination(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
        source: Any,
        destination: Any,
        source_item: StorageItem,
        destination_item: StorageItem,
        destination_path: str,
    ) -> bool:
        expected_size = source_item.size_bytes or destination_item.size_bytes
        unknown_google_export = (
            expected_size is None and source_connection.provider_type == "GOOGLE_DRIVE"
        )
        if expected_size is None and not unknown_google_export:
            return False
        cap = min(
            64 * 1024**2,
            self.settings.transfer_max_file_bytes or 64 * 1024**2,
        )
        staging_size = cap if unknown_google_export else expected_size
        if staging_size is None:
            return False
        staging_reservation = (
            staging_size if self._uses_seafile_api(destination_connection) else staging_size * 2
        )
        async with temporary_staging_area(
            Path(self.settings.transfer_staging_path),
            transfer_id=record.id,
            expected_size=staging_reservation,
            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
            margin_percent=self.settings.transfer_staging_margin_percent,
        ) as area:
            source_area = area / "source"
            source_area.mkdir(mode=0o700)
            download_limit = (
                cap if unknown_google_export else self.settings.transfer_max_file_bytes or None
            )
            source_file = await self._download_versioned_source(
                record,
                source_connection,
                source,
                source_item,
                source_area,
                max_bytes=download_limit,
            )
            if not source_file.is_file() or source_file.is_symlink():
                return False
            actual_size = source_file.stat().st_size
            if source_item.size_bytes is not None and actual_size != source_item.size_bytes:
                return False
            if (
                destination_item.size_bytes is not None
                and actual_size != destination_item.size_bytes
            ):
                return False
            source_digest = await _sha256_file(source_file, self.should_cancel)
            destination_digest = await self._hash_versioned_destination(
                record,
                destination_connection,
                destination,
                destination_item,
                destination_path,
                area,
            )
            return source_digest == destination_digest

    async def _copy_versioned_file(
        self,
        record: TransferRecord,
        source_connection: ConnectionRecord,
        destination_connection: ConnectionRecord,
        source: Any,
        destination: Any,
        item: StorageItem,
        manifest_item: TransferManifestItemRecord,
    ) -> FileCopyResult:
        unknown_google_export = (
            item.size_bytes is None and source_connection.provider_type == "GOOGLE_DRIVE"
        )
        google_export_limit = min(
            64 * 1024**2,
            self.settings.transfer_max_file_bytes or 64 * 1024**2,
        )
        max_bytes = (
            google_export_limit
            if unknown_google_export
            else self.settings.transfer_max_file_bytes or None
        )
        attempts = self.settings.transfer_max_retries + 1
        safe_to_retry_copy = not manifest_item.copy_started
        missing_output_retry_used = False
        for attempt in range(1, attempts + 1):
            if await self.should_cancel():
                await self._cancel(record)
                return FileCopyResult(CopyOutcome.STOPPED)
            record.status = "RUNNING" if attempt == 1 else "RETRYING"
            record.current_item = item.path
            record.current_speed_bps = None
            record.estimated_remaining_seconds = None
            await self.publish(record)
            reserve_size = google_export_limit if unknown_google_export else item.size_bytes
            provider_stage = "SOURCE_DOWNLOAD"
            try:
                async with self._measured_staging_area(
                    record,
                    manifest_item,
                    expected_size=reserve_size,
                ) as area:
                    try:
                        local_file = await self._measure_stage(
                            record,
                            code="SOURCE_DOWNLOAD",
                            connection=source_connection,
                            role="SOURCE",
                            manifest_item=manifest_item,
                            operation=lambda: self._download_versioned_source(
                                record,
                                source_connection,
                                source,
                                item,
                                area,
                                max_bytes=max_bytes,
                            ),
                        )
                    except FileNotFoundError:
                        raise SourceFileError("SOURCE_ITEM_NOT_FOUND") from None
                    if not local_file.is_file() or local_file.is_symlink():
                        raise RuntimeError("Source download did not produce a regular file")
                    actual_size = local_file.stat().st_size
                    if item.size_bytes is not None and actual_size != item.size_bytes:
                        raise SourceFileError("SOURCE_SIZE_MISMATCH")
                    if (
                        self.settings.transfer_max_file_bytes > 0
                        and actual_size > self.settings.transfer_max_file_bytes
                    ):
                        raise ValueError("Source file exceeded the configured size limit")
                    if item.size_bytes is None:
                        await self.store.aupdate_transfer_manifest_item_size(
                            record.id, manifest_item.id, size_bytes=actual_size
                        )
                        manifest_item.size_bytes = actual_size
                        record.bytes_total += actual_size

                    async def hash_source(file_path: Path = local_file) -> str:
                        return await _sha256_file(file_path, self.should_cancel)

                    digest = await self._measure_stage(
                        record,
                        code="SOURCE_HASH",
                        connection=source_connection,
                        role="SOURCE",
                        manifest_item=manifest_item,
                        operation=hash_source,
                        bytes_count=actual_size,
                    )
                    destination_name = PurePosixPath(manifest_item.destination_path).name
                    if (
                        not self._uses_seafile_api(destination_connection)
                        and local_file.name != destination_name
                    ):
                        renamed_local_file = local_file.with_name(destination_name)
                        if renamed_local_file.exists() or renamed_local_file.is_symlink():
                            raise RuntimeError("Temporary destination name is already occupied")
                        local_file.rename(renamed_local_file)
                        local_file = renamed_local_file
                    if not manifest_item.copy_started:
                        await self.store.amark_transfer_manifest_item_started(
                            record.id,
                            manifest_item.id,
                            destination_existed_before=False,
                        )
                        manifest_item.copy_started = True
                    try:
                        provider_stage = "DESTINATION_UPLOAD"
                        # From this point the remote state can be ambiguous for every
                        # provider. Reconcile the destination before accepting success;
                        # never repeat a write merely because it was not immediately listed.
                        safe_to_retry_copy = False

                        async def upload_destination(
                            file_path: Path = local_file,
                            connection: ConnectionRecord = destination_connection,
                            provider: Any = destination,
                            destination_path: str = manifest_item.destination_path,
                        ) -> str | None:
                            return await self._upload_versioned_destination(
                                record,
                                connection,
                                provider,
                                file_path,
                                destination_path,
                            )

                        actual_destination = await self._measure_stage(
                            record,
                            code="DESTINATION_UPLOAD",
                            connection=destination_connection,
                            role="DESTINATION",
                            manifest_item=manifest_item,
                            operation=upload_destination,
                            bytes_count=actual_size,
                        )
                    except Exception as upload_error:
                        existing = await self._find_versioned_destination_item(
                            destination, manifest_item.destination_path
                        )
                        if (
                            existing is not None
                            and not existing.is_dir
                            and existing.size_bytes == actual_size
                        ):
                            try:
                                remote_digest = await self._hash_versioned_destination(
                                    record,
                                    destination_connection,
                                    destination,
                                    existing,
                                    manifest_item.destination_path,
                                    area,
                                )
                            except Exception:
                                remote_digest = None
                            if remote_digest == digest:
                                await self.store.aupdate_transfer_manifest_item(
                                    record.id,
                                    manifest_item.id,
                                    status="COPIED",
                                    verified_by="SHA256_RECONCILED",
                                )
                                await self._refresh_versioned_record(record)
                                record.current_item = item.path
                                record.progress_percent = min(
                                    round(
                                        record.files_completed * 100 / max(record.files_total, 1),
                                        2,
                                    ),
                                    99.99,
                                )
                                await self.publish(record)
                                return FileCopyResult(CopyOutcome.COPIED)
                        if destination_connection.provider_type == "MEGA":
                            raise upload_error from None
                        raise upload_error

                    provider_stage = "DESTINATION_VERIFY"

                    async def verify_destination(
                        actual_destination_path: str | None = actual_destination,
                        destination_path: str = manifest_item.destination_path,
                        file_size: int = actual_size,
                        expected_digest: str = digest,
                    ) -> str:
                        if actual_destination_path != destination_path:
                            raise RuntimeError(
                                "Provider did not create the assigned destination name"
                            )
                        destination_item = await self._find_versioned_destination_item(
                            destination, destination_path
                        )
                        if (
                            destination_item is None
                            or destination_item.is_dir
                            or (
                                destination_item.size_bytes is not None
                                and destination_item.size_bytes != file_size
                            )
                            or (
                                destination_item.size_bytes is None
                                and destination_connection.provider_type != "SEAFILE"
                            )
                        ):
                            raise RuntimeError("Destination size verification failed")
                        if self._uses_seafile_api(destination_connection):
                            remote_digest = await destination.sha256(
                                destination_path,
                                should_cancel=self.should_cancel,
                            )
                            if remote_digest != expected_digest:
                                raise RuntimeError("Destination content verification failed")
                            return "SHA256"
                        return "SIZE"

                    verified_by = await self._measure_stage(
                        record,
                        code="DESTINATION_VERIFY",
                        connection=destination_connection,
                        role="DESTINATION",
                        manifest_item=manifest_item,
                        operation=verify_destination,
                        bytes_count=(
                            actual_size if self._uses_seafile_api(destination_connection) else None
                        ),
                    )
                    await self.store.aupdate_transfer_manifest_item(
                        record.id,
                        manifest_item.id,
                        status="COPIED",
                        verified_by=verified_by,
                    )
                    manifest_item.status = "COPIED"
                    manifest_item.verified_by = verified_by
                    manifest_item.error_code = None
                    await self._refresh_versioned_record(record)
                    record.current_item = item.path
                    if record.bytes_total:
                        record.progress_percent = min(
                            round(record.bytes_transferred * 100 / record.bytes_total, 2),
                            99.99,
                        )
                    else:
                        record.progress_percent = min(
                            round(record.files_completed * 100 / max(record.files_total, 1), 2),
                            99.99,
                        )
                    await self.publish(record)
                    return FileCopyResult(CopyOutcome.COPIED)
            except (
                RcloneCancelledError,
                MegaCmdCancelledError,
                SeafileApiCancelledError,
                GoogleDriveCancelledError,
            ):
                await self._cancel(record)
                return FileCopyResult(CopyOutcome.STOPPED)
            except StagingCleanupError:
                if manifest_item.status == "COPIED":
                    await self._refresh_versioned_record(record)
                    await self._fail(
                        record,
                        error_code="TRANSFER_STAGING_CLEANUP_FAILED",
                        message="No se pudo limpiar el área temporal de la transferencia.",
                        technical_details={
                            "manifest_item_id": str(manifest_item.id),
                            "provider_stage": "STAGING_CLEANUP",
                            "file_attempt": attempt,
                        },
                    )
                else:
                    await self.store.aupdate_transfer_manifest_item(
                        record.id,
                        manifest_item.id,
                        status="FAILED",
                        error_code="TRANSFER_STAGING_CLEANUP_FAILED",
                        failure_stage="STAGING_CLEANUP",
                        failure_attempt=record.attempt_count,
                    )
                    await self._refresh_versioned_record(record)
                    await self._fail(
                        record,
                        error_code="TRANSFER_STAGING_CLEANUP_FAILED",
                        count_error=False,
                        message="No se pudo limpiar el área temporal de la transferencia.",
                        technical_details={
                            "manifest_item_id": str(manifest_item.id),
                            "provider_stage": "STAGING_CLEANUP",
                            "file_attempt": attempt,
                        },
                    )
                return FileCopyResult(CopyOutcome.STOPPED)
            except StagingSpaceError:
                await self.store.aupdate_transfer_manifest_item(
                    record.id,
                    manifest_item.id,
                    status="FAILED",
                    error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT",
                    failure_stage="STAGING",
                    failure_attempt=record.attempt_count,
                )
                await self._refresh_versioned_record(record)
                await self._fail(
                    record,
                    error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT",
                    message="No hay espacio temporal suficiente para transferir el archivo.",
                    technical_details={
                        "file_bytes": item.size_bytes,
                        "manifest_item_id": str(manifest_item.id),
                        "provider_stage": "STAGING",
                        "file_attempt": attempt,
                    },
                )
                return FileCopyResult(CopyOutcome.STOPPED)
            except Exception as exc:
                retryable = bool(getattr(exc, "retryable", False))
                should_retry = retryable and attempt < attempts and safe_to_retry_copy
                if isinstance(exc, MegaCmdDownloadOutputMissingError):
                    should_retry = should_retry and not missing_output_retry_used
                    if should_retry:
                        missing_output_retry_used = True
                if should_retry:
                    retry_code = "TRANSFER_TRANSIENT_ERROR"
                    retry_details = self._versioned_failure_details(
                        exc, provider_stage=provider_stage
                    )
                    if isinstance(exc, MegaCmdDownloadOutputMissingError):
                        retry_code = "MEGACMD_DOWNLOAD_OUTPUT_MISSING"
                        retry_details = self._versioned_failure_details(
                            exc, provider_stage=provider_stage
                        )
                    retry_details.update(
                        provider=(
                            source_connection.provider_type
                            if provider_stage == "SOURCE_DOWNLOAD"
                            else destination_connection.provider_type
                        ),
                        attempt=record.attempt_count,
                        file_attempt=attempt,
                        destination_write_started=manifest_item.copy_started,
                    )
                    if not await self._wait_for_retry(
                        record,
                        min(5 * (2 ** (attempt - 1)) + random.random(), 300),
                        next_attempt=attempt + 1,
                        provider_error_code=retry_code,
                        diagnostic_details=retry_details,
                    ):
                        return FileCopyResult(CopyOutcome.STOPPED)
                    continue
                confirmed_source_missing = (
                    provider_stage == "SOURCE_DOWNLOAD"
                    and not manifest_item.copy_started
                    and await self._confirmed_source_item_missing(source, item, exc)
                )
                if destination_connection.provider_type == "MEGA" and manifest_item.copy_started:
                    code = "MEGA_DESTINATION_STATE_UNCERTAIN"
                elif manifest_item.copy_started:
                    code = "DESTINATION_STATE_UNCERTAIN"
                elif confirmed_source_missing:
                    code = "SOURCE_ITEM_NOT_FOUND"
                elif isinstance(exc, SourceFileError):
                    code = exc.code
                elif isinstance(exc, MegaCmdDownloadValidationError):
                    code = "MEGACMD_DOWNLOAD_OUTPUT_INVALID"
                elif isinstance(exc, MegaCmdListingError):
                    code = "MEGACMD_LISTING_INVALID"
                elif isinstance(exc, MegaCmdDownloadOutputMissingError):
                    code = "MEGACMD_DOWNLOAD_OUTPUT_MISSING"
                elif isinstance(exc, MegaCmdError):
                    code = "MEGACMD_RETRIES_EXHAUSTED" if retryable else "MEGACMD_COMMAND_FAILED"
                else:
                    code = "TRANSFER_FILE_COPY_FAILED"
                await self.store.aupdate_transfer_manifest_item(
                    record.id,
                    manifest_item.id,
                    status="FAILED",
                    error_code=code,
                    failure_stage=provider_stage,
                    failure_attempt=record.attempt_count,
                )
                await self._refresh_versioned_record(record)
                details = self._versioned_failure_details(exc, provider_stage=provider_stage)
                details.update(
                    provider=(
                        source_connection.provider_type
                        if provider_stage == "SOURCE_DOWNLOAD"
                        else destination_connection.provider_type
                    ),
                    operation=provider_stage,
                    manifest_item_id=str(manifest_item.id),
                    attempt=record.attempt_count,
                    file_attempt=attempt,
                    destination_write_started=manifest_item.copy_started,
                )
                if (
                    provider_stage == "SOURCE_DOWNLOAD"
                    and not manifest_item.copy_started
                    and (
                        isinstance(exc, (SourceFileError, MegaCmdDownloadOutputMissingError))
                        or confirmed_source_missing
                    )
                ):
                    await self.store.aadd_transfer_event(
                        record.id,
                        level="ERROR",
                        event_type="FAILURE_DIAGNOSTIC",
                        message="No se pudo copiar este archivo; se continúa con los restantes.",
                        provider_error_code=code,
                        technical_details=details,
                        correlation_id=record.id,
                    )
                    record.status = "RUNNING"
                    await self.publish(record)
                    return FileCopyResult(CopyOutcome.FAILED_SAFE, code, provider_stage)
                await self._fail(
                    record,
                    error_code=code,
                    count_error=False,
                    message="No se pudo copiar y verificar un archivo del manifiesto.",
                    technical_details=details,
                )
                return FileCopyResult(CopyOutcome.STOPPED)
        return FileCopyResult(CopyOutcome.STOPPED)

    @staticmethod
    def _versioned_failure_details(exc: Exception, *, provider_stage: str) -> dict[str, Any]:
        details: dict[str, Any] = {
            "exception_type": type(exc).__name__,
            "provider_stage": provider_stage,
        }
        if isinstance(exc, MegaCmdDownloadOutputMissingError):
            details.update(
                return_code=exc.return_code,
                retryable=exc.retryable,
                expected_output_created=False,
            )
        elif isinstance(exc, MegaCmdCommandError):
            details.update(
                return_code=exc.return_code,
                retryable=exc.retryable,
                safe_failure_code=exc.failure_diagnostic.safe_code,
                failure_category=exc.failure_diagnostic.category.value,
                failure_scope=exc.failure_diagnostic.scope.value,
            )
        elif isinstance(exc, RcloneCommandError):
            details.update(
                return_code=exc.return_code, retryable=exc.retryable, **exc.diagnostic_details
            )
        else:
            details.update(retryable=bool(getattr(exc, "retryable", False)))
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                details["http_status"] = status
        return details

    async def _refresh_versioned_record(self, record: TransferRecord) -> None:
        saved = await self.store.aget_transfer(record.id)
        if saved is None:
            return
        for field_name in (
            "files_total",
            "files_completed",
            "bytes_total",
            "bytes_transferred",
            "warning_count",
            "error_count",
        ):
            setattr(record, field_name, getattr(saved, field_name))

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
                    transfer_record=record,
                    include_selected_folder=True,
                )
            except FileNotFoundError:
                await self._fail(
                    record,
                    error_code="SOURCE_ITEM_NOT_FOUND",
                    message=(
                        "No se encontró uno de los elementos del origen. Compruebe que "
                        "siga disponible y vuelva a intentarlo."
                    ),
                )
                return
            except self._PreflightError as exc:
                await self._fail(
                    record,
                    error_code=exc.error_code,
                    message=exc.message,
                    technical_details=exc.technical_details,
                )
                return

            if not await self._preflight_staging(record, files):
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
                        item_size: int | None = item.size_bytes,
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
                            if item_size is not None:
                                local_file_bytes = min(local_file_bytes, item_size)
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
                        if item_for_api.size_bytes is not None:
                            local_file_bytes = min(local_file_bytes, item_for_api.size_bytes)
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
                            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
                            margin_percent=self.settings.transfer_staging_margin_percent,
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
                            if item.size_bytes is not None and file_size != item.size_bytes:
                                await self._fail(
                                    record,
                                    error_code="SOURCE_SIZE_MISMATCH",
                                    message=(
                                        "El archivo preparado no coincide con el tamaño "
                                        "informado por el origen."
                                    ),
                                    technical_details={
                                        "expected_bytes": item.size_bytes,
                                        "downloaded_bytes": file_size,
                                    },
                                )
                                return
                            source_digest = await _sha256_file(local_file, self.should_cancel)
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
                                        technical_details={"file_name": relative.name},
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
                                            "destination_name_conflict": True,
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
                                if not await self._verify_rclone_destination(
                                    destination, destination_file, file_size
                                ):
                                    await self._fail(
                                        record,
                                        error_code="DESTINATION_VERIFICATION_FAILED",
                                        message=(
                                            "El proveedor no confirmó el archivo copiado. "
                                            "Revise el destino antes de volver a intentarlo."
                                        ),
                                        technical_details={"expected_bytes": file_size},
                                    )
                                    return
                        completed = True
                        break
                    except StagingSpaceError:
                        await self._fail(
                            record,
                            error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT",
                            message=(
                                "No hay espacio temporal suficiente para continuar. "
                                "Libere espacio o contacte al administrador."
                            ),
                            technical_details={
                                "required_file_bytes": item.size_bytes or 0,
                            },
                        )
                        return
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
                        if not await self._wait_for_retry(record, delay, next_attempt=attempt + 1):
                            return
                    if completed:
                        break

                if not completed:
                    await self._fail(record)
                    return
                if not already_identical:
                    record.bytes_transferred = byte_base + (item.size_bytes or 0)
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
                    transfer_record=record,
                    include_selected_folder=True,
                )
            except FileNotFoundError:
                await self._fail(
                    record,
                    error_code="SOURCE_ITEM_NOT_FOUND",
                    message=(
                        "No se encontró uno de los elementos del origen. Compruebe que "
                        "siga disponible y vuelva a intentarlo."
                    ),
                )
                return
            except self._PreflightError as exc:
                await self._fail(
                    record,
                    error_code=exc.error_code,
                    message=exc.message,
                    technical_details=exc.technical_details,
                )
                return

            if not await self._preflight_staging(record, files):
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
                        item_size: int | None = item.size_bytes,
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
                            if item_size is not None:
                                local_file_bytes = min(local_file_bytes, item_size)
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
                            reserve_bytes=self.settings.transfer_staging_reserve_bytes,
                            margin_percent=self.settings.transfer_staging_margin_percent,
                        ) as staging_area:
                            if mega_is_source:
                                local_file = await mega_session.download_file(
                                    item.path,
                                    staging_area,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                                local_file_size = local_file.stat().st_size
                                if item.size_bytes is None:
                                    raise ValueError("Staged file size was not preflighted")
                                if (
                                    item.size_bytes is not None
                                    and local_file_size != item.size_bytes
                                ):
                                    await self._fail(
                                        record,
                                        error_code="SOURCE_SIZE_MISMATCH",
                                        message=(
                                            "El archivo preparado no coincide con el tamaño "
                                            "informado por el origen."
                                        ),
                                        technical_details={
                                            "expected_bytes": item.size_bytes,
                                            "downloaded_bytes": local_file_size,
                                        },
                                    )
                                    return
                                await self.runner.copy_local_to_remote(
                                    local_file,
                                    drive_provider.remote_path(destination_folder),
                                    drive_config,
                                    record.id,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                    conflict_policy=record.conflict_policy,
                                )
                                if not await self._verify_rclone_destination(
                                    drive_provider, destination_file, local_file.stat().st_size
                                ):
                                    await self._fail(
                                        record,
                                        error_code="DESTINATION_VERIFICATION_FAILED",
                                        message=(
                                            "El proveedor no confirmó el archivo copiado. "
                                            "Revise el destino antes de volver a intentarlo."
                                        ),
                                        technical_details={
                                            "expected_bytes": local_file.stat().st_size,
                                        },
                                    )
                                    return
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
                                local_file_size = local_file.stat().st_size
                                if item.size_bytes is None:
                                    raise ValueError("Staged file size was not preflighted")
                                if (
                                    item.size_bytes is not None
                                    and local_file_size != item.size_bytes
                                ):
                                    await self._fail(
                                        record,
                                        error_code="SOURCE_SIZE_MISMATCH",
                                        message=(
                                            "El archivo preparado no coincide con el tamaño "
                                            "informado por el origen."
                                        ),
                                        technical_details={
                                            "expected_bytes": item.size_bytes,
                                            "downloaded_bytes": local_file_size,
                                        },
                                    )
                                    return
                                mega_upload_started = True
                                await mega_session.upload_file(
                                    local_file,
                                    destination_folder,
                                    line_callback=on_progress,
                                    should_cancel=self.should_cancel,
                                )
                    except StagingSpaceError:
                        await self._fail(
                            record,
                            error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT",
                            message=(
                                "No hay espacio temporal suficiente para continuar. "
                                "Libere espacio o contacte al administrador."
                            ),
                            technical_details={
                                "required_file_bytes": item.size_bytes or 0,
                            },
                        )
                        return
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
                        if not await self._wait_for_retry(record, delay, next_attempt=attempt + 1):
                            return
                        continue
                    break

                if item.size_bytes is not None:
                    record.bytes_transferred = byte_base + item.size_bytes
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
        transfer_record: TransferRecord,
        include_selected_folder: bool = False,
        allow_unknown_size: bool = False,
        selected_object_ids: list[str | None] | None = None,
        selected_parent_ids: list[str | None] | None = None,
    ) -> tuple[list[tuple[StorageItem, str]], set[str]]:
        scan_record = replace(
            transfer_record,
            source_paths=selected_paths,
            source_item_ids=selected_object_ids,
            source_parent_ids=selected_parent_ids,
        )

        async def no_progress(_progress: InventoryProgress) -> None:
            return None

        try:
            inventory = await SourceInventoryScanner(self.settings).scan(
                provider,
                scan_record,
                allow_unknown_size=allow_unknown_size,
                maximum_files=self.settings.transfer_max_files,
                on_progress=no_progress,
                should_cancel=self.should_cancel,
                include_selected_folder=include_selected_folder,
            )
        except InventoryPreflightError as exc:
            raise self._PreflightError(
                exc.error_code,
                exc.message,
                exc.technical_details,
            ) from exc
        return inventory.files, {
            relative_path
            for _directory, relative_path in inventory.directories
            if relative_path != "."
        }

    @staticmethod
    def _direct_destination_file_path(
        item_path: str,
        selected_paths: list[str],
        selected_items: list[StorageItem],
        destination_path: str,
    ) -> str | None:
        normalized_item_path = MockStorageProvider.normalize_path(item_path)
        normalized_destination = MockStorageProvider.normalize_path(destination_path)
        for selected_path, selected_item in zip(selected_paths, selected_items, strict=True):
            normalized_selected = MockStorageProvider.normalize_path(selected_path)
            if selected_item.is_dir:
                prefix = normalized_selected.rstrip("/") + "/"
                if normalized_item_path.startswith(prefix):
                    relative_path = normalized_item_path[len(prefix) :]
                    folder_name = PurePosixPath(normalized_selected).name
                    destination_root = normalized_destination
                    if folder_name:
                        destination_root = MockStorageProvider.normalize_path(
                            f"{destination_root.rstrip('/')}/{folder_name}"
                        )
                    return MockStorageProvider.normalize_path(
                        f"{destination_root.rstrip('/')}/{relative_path}"
                    )
            elif normalized_item_path == normalized_selected:
                return MockStorageProvider.normalize_path(
                    f"{normalized_destination.rstrip('/')}/{selected_item.name}"
                )
        return None

    async def _preflight_staging(
        self,
        record: TransferRecord,
        files: list[tuple[StorageItem, str]],
    ) -> bool:
        if not files:
            return True
        unknown_sizes = sum(item.size_bytes is None for item, _path in files)
        if unknown_sizes:
            await self._fail(
                record,
                error_code="TRANSFER_FILE_SIZE_UNKNOWN",
                message=(
                    "No se puede comprobar el espacio temporal porque el origen no informó "
                    "el tamaño de todos los archivos. Consulte al administrador."
                ),
                technical_details={"files_without_size": unknown_sizes},
            )
            return False

        largest_file = max(item.size_bytes or 0 for item, _path in files)
        try:
            async with temporary_staging_area(
                Path(self.settings.transfer_staging_path),
                transfer_id=record.id,
                expected_size=largest_file,
                reserve_bytes=self.settings.transfer_staging_reserve_bytes,
                margin_percent=self.settings.transfer_staging_margin_percent,
            ):
                pass
        except StagingCleanupError:
            await self._fail(
                record,
                error_code="TRANSFER_STAGING_CLEANUP_FAILED",
                message="No se pudo limpiar el área temporal de la transferencia.",
                technical_details={
                    "provider_stage": "STAGING_CLEANUP",
                    "largest_file_bytes": largest_file,
                },
            )
            return False
        except StagingSpaceError:
            await self._fail(
                record,
                error_code="TRANSFER_STAGING_SPACE_INSUFFICIENT",
                message=(
                    "No hay espacio temporal suficiente para procesar el archivo más grande. "
                    "Libere espacio o contacte al administrador."
                ),
                technical_details={"largest_file_bytes": largest_file},
            )
            return False
        return True

    async def _read_rclone_destination(
        self,
        destination: Any,
        path: str,
        expected_size: int,
    ) -> StorageItem | None:
        last_item = None
        for attempt in range(1, 4):
            item = None
            try:
                item = await asyncio.wait_for(destination.get_item(path), timeout=15)
            except Exception:
                pass
            if isinstance(item, StorageItem):
                last_item = item
                if item.is_dir or item.size_bytes == expected_size:
                    return item
            if attempt < 3:
                await self.sleep(float(attempt))
        return last_item

    async def _verify_rclone_destination(
        self,
        destination: Any,
        path: str,
        expected_size: int,
    ) -> bool:
        item = await self._read_rclone_destination(destination, path, expected_size)
        return item is not None and not item.is_dir and item.size_bytes == expected_size

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

    async def _wait_for_retry(
        self,
        record: TransferRecord,
        delay: float,
        *,
        next_attempt: int,
        provider_error_code: str | None = None,
        diagnostic_details: dict[str, Any] | None = None,
    ) -> bool:
        details = {
            "next_attempt": next_attempt,
            "delay_seconds": round(delay, 2),
        }
        if diagnostic_details:
            details.update(diagnostic_details)
        await self.store.aadd_transfer_event(
            record.id,
            level="WARNING",
            event_type="RETRY_SCHEDULED",
            message="La transferencia volverá a intentarlo automáticamente.",
            technical_details=details,
            provider_error_code=provider_error_code,
            correlation_id=record.id,
        )
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
        count_error: bool = True,
    ) -> None:
        stage = record.status
        file_name = PurePosixPath(record.current_item).name if record.current_item else None
        details: dict[str, Any] = {
            "stage": stage,
            "attempt": record.attempt_count,
        }
        if file_name:
            details["file_name"] = file_name[:255]
        details.update(technical_details or {})
        record.status = "FAILED"
        if count_error:
            record.error_count += 1
        record.current_item = None
        record.current_speed_bps = None
        record.completed_at = datetime.now(UTC)
        await self.store.aadd_transfer_event(
            record.id,
            level="ERROR",
            event_type="FAILURE_DIAGNOSTIC",
            message=message,
            technical_details=details,
            provider_error_code=error_code,
            correlation_id=record.id,
        )
        await self.publish(record)


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
