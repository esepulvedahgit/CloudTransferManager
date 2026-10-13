import uuid
from datetime import UTC, datetime

import pytest

from app.services.memory_store import MemoryStore, TransferRecord


def manifest_entry(
    source_path: str, *, is_dir: bool, size_bytes: int | None = None
) -> dict[str, object]:
    return {
        "source_path": source_path,
        "destination_path": f"/archive{source_path}",
        "is_dir": is_dir,
        "size_bytes": size_bytes,
        "modified_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def transfer_record(transfer_id: uuid.UUID) -> TransferRecord:
    return TransferRecord(
        id=transfer_id,
        public_id="TRF-2026-000001",
        requested_by=uuid.uuid4(),
        source_connection_id=uuid.uuid4(),
        destination_connection_id=uuid.uuid4(),
        source_paths=["/"],
        destination_path="/archive",
    )


@pytest.mark.asyncio
async def test_manifest_batches_are_idempotent_and_track_directory_and_file_results():
    store = MemoryStore()
    transfer = transfer_record(uuid.uuid4())
    store.transfers[transfer.id] = transfer
    entries = [
        manifest_entry("/Projects", is_dir=True),
        manifest_entry("/Projects/report.pdf", is_dir=False, size_bytes=4096),
    ]

    inserted = await store.aadd_transfer_manifest_items(transfer.id, entries)
    repeated = await store.aadd_transfer_manifest_items(transfer.id, entries)

    assert inserted == 2
    assert repeated == 0
    directories = await store.alist_pending_scan_directories(transfer.id, limit=200)
    assert [item.source_path for item in directories] == ["/Projects"]
    await store.amark_transfer_directory_scanned(transfer.id, directories[0].id)
    files = await store.alist_transfer_manifest_items(
        transfer.id, statuses={"PENDING"}, after_id=None, limit=200
    )
    assert [item.source_path for item in files] == ["/Projects/report.pdf"]
    await store.amark_transfer_manifest_item_started(
        transfer.id, files[0].id, destination_existed_before=False
    )
    assert files[0].copy_started is True
    assert files[0].destination_existed_before is False

    await store.aupdate_transfer_manifest_item(
        transfer.id, files[0].id, status="COPIED", verified_by="SIZE"
    )
    counts = await store.aget_transfer_manifest_counts(transfer.id)

    assert counts == {"total": 2, "pending": 0, "copied": 1, "skipped": 0, "failed": 0}


@pytest.mark.asyncio
async def test_manifest_supports_more_than_five_thousand_files_in_fixed_size_batches():
    store = MemoryStore()
    transfer = transfer_record(uuid.uuid4())
    store.transfers[transfer.id] = transfer
    entries = [
        manifest_entry(f"/files/{index:05d}.dat", is_dir=False, size_bytes=index)
        for index in range(10_001)
    ]

    inserted = await store.aadd_transfer_manifest_items(transfer.id, entries)
    first_batch = await store.alist_transfer_manifest_items(
        transfer.id, statuses={"PENDING"}, after_id=None, limit=200
    )

    assert inserted == 10_001
    assert len(first_batch) == 200
    assert first_batch[-1].id != first_batch[0].id
