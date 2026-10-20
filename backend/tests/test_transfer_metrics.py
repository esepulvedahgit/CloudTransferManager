import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.database_store import DatabaseStore
from app.services.memory_store import MemoryStore
from app.settings import Settings


def _dashboard_app(role: str = "ADMIN"):
    store = MemoryStore()
    actor = store.add_user(
        username="metrics-reader",
        email="metrics@example.test",
        password_hash=PasswordService().hash("A-Strong-Password123!"),
        role=role,
    )
    other = store.add_user(
        username="other-reader",
        email="other@example.test",
        password_hash=PasswordService().hash("A-Strong-Password456!"),
        role="USER",
    )
    source = store.create_connection("Source MEGA", "MEGA", actor.id)
    destination = store.create_connection("Destination Seafile", "SEAFILE", actor.id)
    other_source = store.create_connection("Other source", "MEGA", other.id)
    other_destination = store.create_connection("Other destination", "SEAFILE", other.id)
    return (
        create_app(
            Settings(app_secret_key="metrics-test-secret", session_cookie_secure=False), store=store
        ),
        store,
        actor,
        other,
        (source, destination, other_source, other_destination),
    )


async def _login(client: httpx.AsyncClient, username: str = "metrics-reader") -> None:
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username, "password": "A-Strong-Password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_metrics_returns_manifest_totals_and_date_coverage():
    app, store, actor, other, connections = _dashboard_app()
    source, destination, other_source, other_destination = connections
    first = store.create_transfer(actor.id, source.id, destination.id, ["/data"], "/backup")
    first.created_at = datetime.now(UTC) - timedelta(days=2)
    await store.aadd_transfer_manifest_items(
        first.id,
        [
            {
                "source_path": "/data/a.bin",
                "destination_path": "/backup/a.bin",
                "is_dir": False,
                "size_bytes": 100,
            },
            {
                "source_path": "/data/b.bin",
                "destination_path": "/backup/b.bin",
                "is_dir": False,
                "size_bytes": 20,
            },
            {
                "source_path": "/data/folder",
                "destination_path": "/backup/folder",
                "is_dir": True,
                "size_bytes": None,
            },
        ],
    )
    manifest = await store.alist_transfer_manifest_items(first.id)
    for item in manifest:
        await store.aupdate_transfer_manifest_item(
            first.id, item.id, status="COPIED", verified_by="SHA256"
        )
    second = store.create_transfer(
        other.id, other_source.id, other_destination.id, ["/private"], "/private-copy"
    )
    second.created_at = datetime.now(UTC) - timedelta(days=1)
    await store.aadd_transfer_manifest_items(
        second.id,
        [
            {
                "source_path": "/private/secret.bin",
                "destination_path": "/private-copy/secret.bin",
                "is_dir": False,
                "size_bytes": 999,
            }
        ],
    )
    private_item = (await store.alist_transfer_manifest_items(second.id))[0]
    await store.aupdate_transfer_manifest_item(second.id, private_item.id, status="COPIED")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client)
        response = await client.get(f"/api/v1/metrics/dashboard?range=30d&user_id={actor.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["transfers"]["total"] == 1
    assert payload["summary"]["files"]["copied"] == 2
    assert payload["summary"]["files"]["copied_bytes"] == 120
    assert payload["coverage"]["manifestless_transfers"] == 0
    assert payload["system"]["status"] == "SIMULATED"
    assert "secret.bin" not in response.text


@pytest.mark.asyncio
async def test_non_admin_cannot_filter_dashboard_to_another_users_metrics():
    app, _store, _actor, other, _connections = _dashboard_app("USER")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client)
        response = await client.get(f"/api/v1/metrics/dashboard?user_id={other.id}")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_filter_dashboard_to_one_users_metrics():
    app, store, actor, other, connections = _dashboard_app()
    source, destination, other_source, other_destination = connections
    store.create_transfer(actor.id, source.id, destination.id, ["/mine"], "/mine")
    store.create_transfer(other.id, other_source.id, other_destination.id, ["/theirs"], "/theirs")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client)
        response = await client.get(f"/api/v1/metrics/dashboard?user_id={other.id}")

    assert response.status_code == 200
    assert response.json()["summary"]["transfers"]["total"] == 1


@pytest.mark.asyncio
async def test_database_metrics_use_aggregates_and_record_exact_copy_date(tmp_path):
    store = DatabaseStore(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'metrics.sqlite'}")
    )
    await store.create_schema()
    user = await store.acreate_user("metrics-db", "metrics-db@example.test", "hash", "ADMIN")
    source = await store.acreate_connection("Source", "MEGA", user.id, uuid.uuid4(), b"encrypted")
    destination = await store.acreate_connection(
        "Destination", "SEAFILE", user.id, uuid.uuid4(), b"encrypted"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/data"], "/backup"
    )
    now = datetime.now(UTC)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/data/a.rar",
                "destination_path": "/backup/a.rar",
                "is_dir": False,
                "size_bytes": 512,
            },
            {"source_path": "/data/folder", "destination_path": "/backup/folder", "is_dir": True},
        ],
    )
    items = await store.alist_transfer_manifest_items(transfer.id)
    source_item = next(item for item in items if not item.is_dir)
    for item in items:
        if not item.is_dir:
            await store.aupdate_transfer_manifest_item(transfer.id, item.id, status="COPIED")
    span_id = await store.astart_transfer_metric_span(
        transfer.id,
        dispatch_token=None,
        attempt_count=transfer.attempt_count,
        span_type="STAGE",
        code="SOURCE_DOWNLOAD",
        manifest_item_id=source_item.id,
        connection_id=source.id,
        provider_type="MEGA",
        connection_role="SOURCE",
        started_at=now,
    )
    assert span_id is not None
    assert await store.afinish_transfer_metric_span(
        span_id,
        dispatch_token=None,
        completed_at=now + timedelta(seconds=2),
        duration_ms=2000,
        outcome="SUCCESS",
        bytes_count=512,
    )
    try:
        payload = await store.ametric_dashboard(
            requested_by=user.id,
            date_from=now - timedelta(days=1),
            date_to=now + timedelta(days=1),
            timezone="UTC",
        )
    finally:
        await store.aclose()

    assert payload["summary"]["files"]["copied"] == 1
    assert payload["summary"]["files"]["copied_bytes"] == 512
    assert payload["coverage"]["copied_files_with_date"] == 1
    assert payload["reliability"]["extensions"] == [{"extension": "rar", "files": 1}]
    assert payload["performance"]["stages"][0]["median_ms"] == 2000
    assert payload["performance"]["stages"][0]["effective_bytes_per_second"] == 256


@pytest.mark.asyncio
async def test_memory_metrics_fence_spans_from_old_worker_attempts():
    store = MemoryStore()
    user = store.add_user(
        username="metrics-fence",
        email="metrics-fence@example.test",
        password_hash="hash",
        role="ADMIN",
    )
    source = store.create_connection("Source", "MEGA", user.id)
    destination = store.create_connection("Destination", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/data"], "/backup")
    current_token = uuid.uuid4()
    transfer.dispatch_token = current_token

    stale = await store.astart_transfer_metric_span(
        transfer.id,
        dispatch_token=uuid.uuid4(),
        attempt_count=transfer.attempt_count,
        span_type="STAGE",
        code="SOURCE_DOWNLOAD",
    )
    current = await store.astart_transfer_metric_span(
        transfer.id,
        dispatch_token=current_token,
        attempt_count=transfer.attempt_count,
        span_type="STAGE",
        code="SOURCE_DOWNLOAD",
    )

    assert stale is None
    assert current is not None
    transfer.attempt_count += 1
    assert not await store.afinish_transfer_metric_span(
        current,
        dispatch_token=current_token,
        completed_at=datetime.now(UTC),
        duration_ms=100,
        outcome="SUCCESS",
    )


@pytest.mark.asyncio
async def test_postgresql_metrics_use_native_aggregates_and_percentiles():
    database_url = os.getenv("CTM_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("CTM_TEST_POSTGRES_URL is required for PostgreSQL metrics coverage")
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    suffix = uuid.uuid4().hex[:12]
    user = await store.acreate_user(
        f"metrics-pg-{suffix}", f"metrics-pg-{suffix}@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("PG source", "MEGA", user.id, uuid.uuid4(), b"secret")
    destination = await store.acreate_connection(
        "PG destination", "SEAFILE", user.id, uuid.uuid4(), b"secret"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/data"], "/backup"
    )
    now = datetime.now(UTC)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/data/archive.tar.gz",
                "destination_path": "/backup/archive.tar.gz",
                "is_dir": False,
                "size_bytes": 1000,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]
    await store.aupdate_transfer_manifest_item(transfer.id, item.id, status="COPIED")
    span_id = await store.astart_transfer_metric_span(
        transfer.id,
        dispatch_token=None,
        attempt_count=transfer.attempt_count,
        span_type="STAGE",
        code="SOURCE_DOWNLOAD",
        manifest_item_id=item.id,
        connection_id=source.id,
        provider_type="MEGA",
        connection_role="SOURCE",
        started_at=now,
    )
    assert span_id is not None
    await store.afinish_transfer_metric_span(
        span_id,
        dispatch_token=None,
        completed_at=now + timedelta(seconds=1),
        duration_ms=1000,
        outcome="SUCCESS",
        bytes_count=1000,
    )
    try:
        payload = await store.ametric_dashboard(
            requested_by=user.id,
            date_from=now - timedelta(days=1),
            date_to=now + timedelta(days=1),
            timezone="UTC",
        )
    finally:
        await store.aclose()

    assert payload["summary"]["files"]["copied"] == 1
    assert payload["reliability"]["extensions"] == [{"extension": "gz", "files": 1}]
    assert payload["performance"]["stages"][0]["median_ms"] == 1000
