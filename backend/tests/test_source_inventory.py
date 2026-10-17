from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.providers.base import PaginatedItems, StorageItem
from app.services.memory_store import TransferRecord
from app.services.source_inventory import (
    InventoryCancelledError,
    InventoryPreflightError,
    InventoryProgress,
    SourceInventoryScanner,
)
from app.settings import Settings


def item(
    path: str,
    *,
    is_dir: bool = False,
    object_id: str | None = None,
    size: int | None = 1,
) -> StorageItem:
    return StorageItem(
        name=path.rsplit("/", 1)[-1] or "/",
        path=path,
        is_dir=is_dir,
        object_id=object_id,
        size_bytes=size,
        provider_type="MOCK",
    )


class InventoryProvider:
    def __init__(self, roots: list[StorageItem], pages: dict[str, list[list[StorageItem]]]):
        self.roots = roots
        self.pages = pages
        self.list_calls: list[tuple[str, str | None]] = []

    async def get_item(
        self, path: str, object_id: str | None = None, parent_object_id: str | None = None
    ) -> StorageItem | None:
        del parent_object_id
        return next(
            (
                root
                for root in self.roots
                if root.path == path and (object_id is None or root.object_id == object_id)
            ),
            None,
        )

    async def list_items(
        self,
        path: str,
        cursor: str | None = None,
        page_size: int = 200,
        **kwargs: Any,
    ) -> PaginatedItems:
        del kwargs, page_size
        self.list_calls.append((path, cursor))
        pages = self.pages.get(path, [])
        index = int(cursor or "0")
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return PaginatedItems(
            items=pages[index] if index < len(pages) else [],
            next_cursor=next_cursor,
        )


def transfer(paths: list[str], ids: list[str | None] | None = None) -> TransferRecord:
    return TransferRecord(
        id=uuid.uuid4(),
        public_id="TRF-2026-000001",
        requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(),
        destination_connection_id=uuid.uuid4(),
        source_paths=paths,
        source_item_ids=ids,
        destination_path="/destination",
    )


async def scan(
    provider: InventoryProvider,
    record: TransferRecord,
    *,
    maximum_files: int = 5000,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
):
    progress: list[InventoryProgress] = []

    async def on_progress(value: InventoryProgress) -> None:
        progress.append(value)

    async def never_cancel() -> bool:
        return False

    scanner = SourceInventoryScanner(Settings(transfer_max_file_bytes=0))
    result = await scanner.scan(
        provider,
        record,
        allow_unknown_size=False,
        maximum_files=maximum_files,
        on_progress=on_progress,
        should_cancel=should_cancel or never_cancel,
    )
    return result, progress


@pytest.mark.asyncio
async def test_exact_file_limit_is_accepted_and_count_is_complete():
    root = item("/root", is_dir=True, object_id="root")
    files = [item(f"/root/{index}.dat", object_id=f"file-{index}") for index in range(5000)]
    provider = InventoryProvider([root], {"/root": [files]})

    inventory, progress = await scan(provider, transfer(["/root"]), maximum_files=5000)

    assert len(inventory.files) == 5000
    assert progress[-1] == InventoryProgress(files_found=5000, count_complete=True)


@pytest.mark.asyncio
async def test_file_above_limit_stops_at_limit_plus_one_with_partial_count():
    root = item("/root", is_dir=True, object_id="root")
    files = [item(f"/root/{index}.dat", object_id=f"file-{index}") for index in range(5001)]
    provider = InventoryProvider([root], {"/root": [files]})

    with pytest.raises(InventoryPreflightError) as caught:
        await scan(provider, transfer(["/root"]), maximum_files=5000)

    assert caught.value.error_code == "TRANSFER_FILE_LIMIT_EXCEEDED"
    assert caught.value.progress == InventoryProgress(files_found=5001, count_complete=False)
    assert provider.list_calls == [("/root", None)]


@pytest.mark.asyncio
async def test_directories_do_not_count_as_files():
    root = item("/root", is_dir=True, object_id="root")
    folder = item("/root/sub", is_dir=True, object_id="sub")
    file = item("/root/sub/file.dat", object_id="file")
    provider = InventoryProvider([root], {"/root": [[folder]], "/root/sub": [[file]]})

    inventory, progress = await scan(provider, transfer(["/root"]), maximum_files=1)

    assert len(inventory.files) == 1
    assert len(inventory.directories) == 2
    assert progress[-1] == InventoryProgress(files_found=1, count_complete=True)


@pytest.mark.asyncio
async def test_overlapping_selected_directories_count_each_object_once():
    root = item("/root", is_dir=True, object_id="root")
    nested = item("/root/sub", is_dir=True, object_id="sub")
    file = item("/root/sub/file.dat", object_id="file")
    provider = InventoryProvider(
        [root, nested], {"/root": [[nested]], "/root/sub": [[file]]}
    )

    inventory, progress = await scan(
        provider, transfer(["/root", "/root/sub"]), maximum_files=1
    )

    assert len(inventory.files) == 1
    assert progress[-1] == InventoryProgress(files_found=1, count_complete=True)


@pytest.mark.asyncio
async def test_homonymous_files_with_distinct_object_ids_are_counted_separately():
    root = item("/root", is_dir=True, object_id="root")
    first = item("/root/report.txt", object_id="first")
    second = item("/root/report.txt", object_id="second")
    provider = InventoryProvider([root], {"/root": [[first, second]]})

    inventory, progress = await scan(provider, transfer(["/root"]), maximum_files=2)

    assert len(inventory.files) == 2
    assert progress[-1] == InventoryProgress(files_found=2, count_complete=True)


@pytest.mark.asyncio
async def test_repeated_cursor_fails_closed():
    root = item("/root", is_dir=True, object_id="root")

    class RepeatingCursorProvider(InventoryProvider):
        async def list_items(self, path, cursor=None, page_size=200, **kwargs):
            del kwargs, page_size
            self.list_calls.append((path, cursor))
            return PaginatedItems(items=[], next_cursor="repeat")

    provider = RepeatingCursorProvider([root], {})
    with pytest.raises(ValueError, match="repeated page cursor"):
        await scan(provider, transfer(["/root"]))


@pytest.mark.asyncio
async def test_empty_inventory_finishes_as_complete_zero():
    root = item("/root", is_dir=True, object_id="root")
    provider = InventoryProvider([root], {"/root": [[]]})

    inventory, progress = await scan(provider, transfer(["/root"]))

    assert inventory.files == []
    assert progress[-1] == InventoryProgress(files_found=0, count_complete=True)


@pytest.mark.asyncio
async def test_cancel_during_listing_stops_inventory():
    root = item("/root", is_dir=True, object_id="root")
    provider = InventoryProvider([root], {"/root": [[]]})

    async def cancel() -> bool:
        return True

    with pytest.raises(InventoryCancelledError, match="cancel"):
        await scan(provider, transfer(["/root"]), should_cancel=cancel)


@pytest.mark.asyncio
async def test_unknown_size_and_per_file_size_limit_fail_with_partial_progress():
    root = item("/root", is_dir=True, object_id="root")
    unknown = item("/root/unknown", object_id="unknown", size=None)
    provider = InventoryProvider([root], {"/root": [[unknown]]})

    with pytest.raises(InventoryPreflightError) as caught:
        await scan(provider, transfer(["/root"]))
    assert caught.value.error_code == "TRANSFER_FILE_SIZE_UNKNOWN"

    large = item("/root/large", object_id="large", size=9)
    large_provider = InventoryProvider([root], {"/root": [[large]]})
    scanner = SourceInventoryScanner(Settings(transfer_max_file_bytes=8))

    async def on_progress(_value: InventoryProgress) -> None:
        return None

    async def never_cancel() -> bool:
        return False

    with pytest.raises(InventoryPreflightError) as size_caught:
        await scanner.scan(
            large_provider,
            transfer(["/root"]),
            allow_unknown_size=False,
            maximum_files=5000,
            on_progress=on_progress,
            should_cancel=never_cancel,
        )
    assert size_caught.value.error_code == "TRANSFER_FILE_SIZE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_partial_progress_is_emitted_as_files_are_discovered():
    root = item("/root", is_dir=True, object_id="root")
    files = [item(f"/root/{index}.dat", object_id=f"file-{index}") for index in range(3)]
    provider = InventoryProvider([root], {"/root": [[files[0]], files[1:]]})

    _inventory, progress = await scan(provider, transfer(["/root"]))

    assert [value.files_found for value in progress] == [1, 2, 3, 3]
    assert progress[-1].count_complete is True
    assert provider.list_calls == [("/root", None), ("/root", "1")]
