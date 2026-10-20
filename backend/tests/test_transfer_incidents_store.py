import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.models import TransferManifestItem
from app.services.database_store import DatabaseStore
from app.settings import Settings


@pytest.mark.asyncio
async def test_database_store_pages_recent_incidents_and_counts_only_explicit_omissions(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'incidents.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("incident-db", "incident-db@example.test", "hash", "ADMIN")
    source = await store.acreate_connection("MEGA cuenta", "MEGA", user.id, uuid.uuid4(), b"s")
    destination = await store.acreate_connection(
        "Biblioteca Seafile", "SEAFILE", user.id, uuid.uuid4(), b"d"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Cursos"], "/Backup"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {"source_path": "/Cursos/1/igual.rar", "destination_path": "/Backup/1/igual.rar"},
            {"source_path": "/Cursos/2/igual.rar", "destination_path": "/Backup/2/igual.rar"},
            {"source_path": "/Cursos/omitido.rar", "destination_path": "/Backup/omitido.rar"},
            {"source_path": "/Cursos/legacy.rar", "destination_path": "/Backup/legacy.rar"},
        ],
    )
    rows = await store.alist_transfer_manifest_items(transfer.id, limit=10)
    by_path = {item.source_path: item for item in rows}
    stamp = datetime.now(UTC)
    async with store.sessions() as session:
        async with session.begin():
            await session.execute(
                update(TransferManifestItem)
                .where(TransferManifestItem.id == by_path["/Cursos/1/igual.rar"].id)
                .values(status="FAILED", updated_at=stamp - timedelta(minutes=1))
            )
            await session.execute(
                update(TransferManifestItem)
                .where(TransferManifestItem.id == by_path["/Cursos/2/igual.rar"].id)
                .values(status="FAILED", updated_at=stamp)
            )
            await session.execute(
                update(TransferManifestItem)
                .where(TransferManifestItem.id == by_path["/Cursos/omitido.rar"].id)
                .values(
                    status="SKIPPED",
                    omission_kind="USER_ACCEPTED_SOURCE_FAILURE",
                    omitted_at=stamp,
                    updated_at=stamp,
                )
            )
            await session.execute(
                update(TransferManifestItem)
                .where(TransferManifestItem.id == by_path["/Cursos/legacy.rar"].id)
                .values(status="SKIPPED", updated_at=stamp)
            )

    try:
        counts = await store.acount_transfer_incidents(None)
        first_failed = await store.alist_transfer_incidents(None, kind="failed", limit=1)
        second_failed = await store.alist_transfer_incidents(
            None,
            kind="failed",
            before_at=first_failed[0].incident_at,
            before_id=first_failed[0].item.id,
            limit=1,
        )
        omitted = await store.alist_transfer_incidents(None, kind="omitted", limit=10)
    finally:
        await store.aclose()

    assert counts == {"failed": 2, "omitted": 1}
    assert first_failed[0].item.source_path == "/Cursos/2/igual.rar"
    assert first_failed[0].source_connection.name == "MEGA cuenta"
    assert first_failed[0].destination_connection.name == "Biblioteca Seafile"
    assert second_failed[0].item.source_path == "/Cursos/1/igual.rar"
    assert [row.item.source_path for row in omitted] == ["/Cursos/omitido.rar"]
