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
async def test_manifest_distinguishes_same_path_objects_and_keeps_first_assignment():
    store = MemoryStore()
    transfer = transfer_record(uuid.uuid4())
    store.transfers[transfer.id] = transfer
    entries = [
        {
            **manifest_entry("/folder/report.txt", is_dir=False, size_bytes=10),
            "source_object_id": "H:First123",
            "requested_destination_path": "/archive/report.txt",
            "destination_path": "/archive/report.txt",
        },
        {
            **manifest_entry("/folder/report.txt", is_dir=False, size_bytes=12),
            "source_object_id": "H:Second12",
            "requested_destination_path": "/archive/report.txt",
            "destination_path": "/archive/report_01.txt",
            "rename_reason": "NAME_COLLISION",
        },
    ]

    assert await store.aadd_transfer_manifest_items(transfer.id, entries) == 2
    changed_retry = {
        **entries[0],
        "destination_path": "/archive/report_99.txt",
        "rename_reason": "NAME_COLLISION",
    }
    assert await store.aadd_transfer_manifest_items(transfer.id, [changed_retry]) == 0
    items = await store.alist_transfer_manifest_items(
        transfer.id,
        statuses={"PENDING"},
        limit=10,
    )

    assert {(item.source_object_id, item.destination_path) for item in items} == {
        ("H:First123", "/archive/report.txt"),
        ("H:Second12", "/archive/report_01.txt"),
    }


@pytest.mark.asyncio
async def test_memory_store_live_manifest_progress_does_not_count_failed_empty_files_as_copied():
    store = MemoryStore()
    transfer = transfer_record(uuid.uuid4())
    transfer.files_total = 1
    store.transfers[transfer.id] = transfer
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [manifest_entry("/empty.bin", is_dir=False, size_bytes=0)],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]

    await store.aupdate_transfer_manifest_item(transfer.id, item.id, status="FAILED")

    refreshed = await store.aget_transfer(transfer.id)
    assert refreshed is not None
    assert refreshed.progress_percent < 100


@pytest.mark.asyncio
async def test_memory_store_aggregates_file_manifest_summaries_without_directories():
    store = MemoryStore()
    transfer = transfer_record(uuid.uuid4())
    store.transfers[transfer.id] = transfer
    entries = [
        manifest_entry("/folder", is_dir=True),
        manifest_entry("/copied.bin", is_dir=False, size_bytes=4),
        manifest_entry("/failed.bin", is_dir=False, size_bytes=3),
        manifest_entry("/skipped.bin", is_dir=False, size_bytes=2),
        manifest_entry("/pending.bin", is_dir=False, size_bytes=1),
    ]
    await store.aadd_transfer_manifest_items(transfer.id, entries)
    items = await store.alist_transfer_manifest_items(
        transfer.id,
        statuses={"PENDING"},
        after_id=None,
        limit=10,
    )
    statuses = {
        "/copied.bin": "COPIED",
        "/failed.bin": "FAILED",
        "/skipped.bin": "SKIPPED",
    }
    for item in items:
        if item.source_path in statuses:
            await store.aupdate_transfer_manifest_item(
                transfer.id, item.id, status=statuses[item.source_path]
            )

    missing_id = uuid.uuid4()
    summaries = await store.alist_transfer_manifest_summaries([transfer.id, missing_id])

    assert summaries[transfer.id] == {
        "copied": 1,
        "failed": 1,
        "skipped": 1,
        "pending": 1,
        "renamed": 0,
        "omitted": 0,
        "omitted_bytes": 0,
    }
    assert summaries[missing_id] == {
        "copied": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
        "renamed": 0,
        "omitted": 0,
        "omitted_bytes": 0,
    }


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
