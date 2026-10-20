from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime

import pytest

from app.services.memory_store import MemoryStore
from app.services.transfer_omission_report import iter_omissions_csv


@pytest.mark.asyncio
async def test_omission_csv_is_paged_escaped_and_safe_for_spreadsheets():
    store = MemoryStore()
    transfer = store.create_transfer(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), ["/source"], "/destination"
    )
    transfer_id = transfer.id
    entries = []
    for index in range(205):
        name = f"file-{index:03}.txt"
        entries.append(
            {
                "source_path": f"/source/{name}",
                "destination_path": f"/destination/{name}",
                "size_bytes": index + 1,
            }
        )
    entries[0]["source_path"] = '/source/=HYPERLINK("https://example.invalid")\narchivo ü.txt'
    entries[1]["source_path"] = "/source/+SUM(1,1)"
    entries[2]["source_path"] = "/source/-1"
    entries[3]["source_path"] = "/source/@SUM(1,1)"
    entries[4]["source_path"] = "/source/\tformula"
    entries[5]["source_path"] = "/source/\rformula"
    await store.aadd_transfer_manifest_items(transfer_id, entries)
    rows = await store.alist_transfer_manifest_items(transfer_id, limit=300)
    for item in rows:
        item.status = "SKIPPED"
        if item.source_path.endswith(("file-203.txt", "file-204.txt")):
            continue
        item.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
        item.omitted_at = datetime.now(UTC)

    page_sizes = []
    original_list = store.alist_transfer_manifest_items

    async def track_pages(*args, **kwargs):
        page_sizes.append(kwargs.get("limit"))
        return await original_list(*args, **kwargs)

    store.alist_transfer_manifest_items = track_pages
    output = "".join([chunk async for chunk in iter_omissions_csv(store, transfer_id)])
    parsed = list(csv.reader(io.StringIO(output)))
    by_source = {row[0]: row for row in parsed[1:]}

    assert parsed[0] == [
        "ruta_origen",
        "destino_asignado",
        "motivo",
        "fecha_omision",
        "tamano_bytes",
    ]
    assert len(parsed) == 204
    assert by_source["'/source/=HYPERLINK(\"https://example.invalid\")\narchivo ü.txt"][4] == "1"
    assert by_source["'/source/+SUM(1,1)"][0].startswith("'")
    assert by_source["'/source/-1"][0].startswith("'")
    assert by_source["'/source/@SUM(1,1)"][0].startswith("'")
    assert by_source["'/source/\tformula"][0].startswith("'")
    assert by_source["'/source/\rformula"][0].startswith("'")
    assert all(size == 200 for size in page_sizes)
