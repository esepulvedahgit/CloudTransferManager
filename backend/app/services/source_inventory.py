from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from app.providers.base import StorageItem
from app.providers.mock import MockStorageProvider
from app.services.memory_store import TransferRecord
from app.settings import Settings


@dataclass(frozen=True)
class InventoryProgress:
    files_found: int
    count_complete: bool


@dataclass
class SourceInventory:
    files: list[tuple[StorageItem, str]]
    directories: list[tuple[StorageItem, str]]


class InventoryPreflightError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        progress: InventoryProgress,
        technical_details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.progress = progress
        self.technical_details = technical_details or {}


class InventoryCancelledError(Exception):
    """Raised when the owner cancels a read-only source inventory scan."""


class SourceInventoryScanner:
    """Enumerate a transfer's selected source tree without writing or downloading data."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scan(
        self,
        source: Any,
        record: TransferRecord,
        *,
        allow_unknown_size: bool,
        maximum_files: int,
        on_progress: Callable[[InventoryProgress], Awaitable[None]],
        should_cancel: Callable[[], Awaitable[bool]],
        include_selected_folder: bool = True,
    ) -> SourceInventory:
        if maximum_files < 1:
            raise ValueError("maximum_files must be at least one")

        files: dict[str, tuple[StorageItem, str]] = {}
        directories: dict[str, tuple[StorageItem, str]] = {}
        visited_directories: set[str] = set()

        def current_progress(*, complete: bool = False) -> InventoryProgress:
            return InventoryProgress(files_found=len(files), count_complete=complete)

        def validate_name(item: StorageItem) -> None:
            if (
                not item.name
                or item.name in {".", ".."}
                or "/" in item.name
                or "\\" in item.name
                or any(ord(character) < 32 for character in item.name)
            ):
                raise ValueError("Provider returned an invalid item name")

        def preflight_error(
            code: str,
            message: str,
            details: dict[str, Any] | None = None,
        ) -> InventoryPreflightError:
            return InventoryPreflightError(code, message, current_progress(), details)

        async def check_cancelled() -> None:
            if await should_cancel():
                raise InventoryCancelledError("Source inventory cancelled")

        async def add_directory(item: StorageItem, relative: PurePosixPath) -> None:
            validate_name(item)
            key = self._identity(item)
            if key in directories:
                return
            if any(
                existing.path == item.path and (not existing.object_id or not item.object_id)
                for existing, _relative in directories.values()
            ):
                raise preflight_error(
                    "SOURCE_OBJECT_ID_UNAVAILABLE",
                    "El origen devolvió carpetas homónimas sin identificadores únicos.",
                    {"source_path": PurePosixPath(item.path).name},
                )
            directories[key] = (item, relative.as_posix())

        async def add_file(item_path: str, item: StorageItem, relative_path: str) -> None:
            identity = self._identity(item, fallback_path=item_path)
            if identity in files:
                return
            duplicate_path_without_identity = any(
                existing_item.path == item_path
                and (not item.object_id or not existing_item.object_id)
                for existing_item, _existing_relative in files.values()
            )
            if duplicate_path_without_identity:
                raise preflight_error(
                    "SOURCE_OBJECT_ID_UNAVAILABLE",
                    "El origen devolvió elementos con la misma ruta sin identificadores únicos.",
                    {"source_path": PurePosixPath(item_path).name},
                )

            # Match the copy preflight order: file count is checked before file size.
            if len(files) >= maximum_files:
                files[identity] = (item, relative_path)
                progress = current_progress()
                await on_progress(progress)
                raise InventoryPreflightError(
                    "TRANSFER_FILE_LIMIT_EXCEEDED",
                    (
                        f"La selección supera el máximo de {maximum_files:,} archivos. "
                        "Divida la copia en varias transferencias."
                    ),
                    progress,
                    {"maximum_files": maximum_files},
                )

            maximum_file_bytes = self.settings.transfer_max_file_bytes
            if maximum_file_bytes > 0 and item.size_bytes is None and not allow_unknown_size:
                raise preflight_error(
                    "TRANSFER_FILE_SIZE_UNKNOWN",
                    (
                        "No se puede comprobar el límite porque el origen no informó "
                        "el tamaño de todos los archivos. Consulte al administrador."
                    ),
                    {"maximum_file_bytes": maximum_file_bytes},
                )
            if (
                maximum_file_bytes > 0
                and item.size_bytes is not None
                and item.size_bytes > maximum_file_bytes
            ):
                raise preflight_error(
                    "TRANSFER_FILE_SIZE_LIMIT_EXCEEDED",
                    (
                        "Un archivo supera el tamaño máximo permitido para una transferencia. "
                        "Divida la copia o ajuste el límite con el administrador."
                    ),
                    {
                        "maximum_file_bytes": maximum_file_bytes,
                        "file_bytes": item.size_bytes,
                    },
                )
            if item.size_bytes is None and not allow_unknown_size:
                raise preflight_error(
                    "TRANSFER_FILE_SIZE_UNKNOWN",
                    "El origen no informó el tamaño de todos los archivos.",
                )

            files[identity] = (item, relative_path)
            await on_progress(current_progress())

        async def walk(directory: StorageItem, relative_directory: PurePosixPath) -> None:
            await check_cancelled()
            identity = self._identity(directory)
            if identity in visited_directories:
                return
            visited_directories.add(identity)
            cursor: str | None = None
            seen_cursors: set[str] = set()
            directory_path = MockStorageProvider.normalize_path(directory.path)
            listed_children: dict[str, StorageItem] = {}
            while True:
                await check_cancelled()
                page = await source.list_items(
                    directory_path,
                    cursor=cursor,
                    page_size=200,
                    parent_object_id=directory.object_id,
                )
                for child in page.items:
                    await check_cancelled()
                    child_path = MockStorageProvider.normalize_path(child.path)
                    if (child_path.rpartition("/")[0] or "/") != directory_path:
                        raise ValueError("Provider returned an item outside the requested folder")
                    prior_child = listed_children.get(child_path)
                    if prior_child is not None and (
                        not prior_child.object_id or not child.object_id
                    ):
                        raise preflight_error(
                            "SOURCE_OBJECT_ID_UNAVAILABLE",
                            "El origen devolvió elementos homónimos sin identificadores únicos.",
                            {"source_path": PurePosixPath(child_path).name},
                        )
                    if prior_child is not None and prior_child.object_id != child.object_id:
                        listed_children[child_path] = child
                    else:
                        listed_children.setdefault(child_path, child)

                    validate_name(child)
                    relative_child = relative_directory / child.name
                    if child.is_dir:
                        await add_directory(child, relative_child)
                        await walk(child, relative_child)
                    else:
                        await add_file(child_path, child, relative_child.as_posix())

                if page.next_cursor is None:
                    break
                if page.next_cursor in seen_cursors:
                    raise ValueError("Provider returned a repeated page cursor")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor

        if record.source_item_ids is not None and len(record.source_item_ids) != len(
            record.source_paths
        ):
            raise ValueError("Selected source paths and object identifiers do not match")
        if record.source_parent_ids is not None and len(record.source_parent_ids) != len(
            record.source_paths
        ):
            raise ValueError("Selected source paths and parent identifiers do not match")

        for index, requested_path in enumerate(record.source_paths):
            await check_cancelled()
            normalized = MockStorageProvider.normalize_path(requested_path)
            object_id = (
                record.source_item_ids[index] if record.source_item_ids is not None else None
            )
            parent_object_id = (
                record.source_parent_ids[index] if record.source_parent_ids is not None else None
            )
            if object_id is not None or parent_object_id is not None:
                selected = await source.get_item(
                    normalized,
                    object_id=object_id,
                    parent_object_id=parent_object_id,
                )
            else:
                selected = await source.get_item(normalized)
            if selected is None:
                raise FileNotFoundError("Source item was not found")
            validate_name(selected)
            if MockStorageProvider.normalize_path(selected.path) != normalized:
                raise ValueError("Provider returned a different source path")
            if selected.is_dir:
                relative_root = (
                    PurePosixPath(selected.name)
                    if include_selected_folder
                    else PurePosixPath()
                )
                if relative_root.parts:
                    await add_directory(selected, relative_root)
                await walk(selected, relative_root)
            else:
                await add_file(normalized, selected, selected.name)

        await on_progress(current_progress(complete=True))
        return SourceInventory(files=list(files.values()), directories=list(directories.values()))

    @staticmethod
    def _identity(item: StorageItem, *, fallback_path: str | None = None) -> str:
        if item.object_id:
            return f"object:{item.object_id}"
        path = fallback_path if fallback_path is not None else item.path
        return f"path:{MockStorageProvider.normalize_path(path)}"
