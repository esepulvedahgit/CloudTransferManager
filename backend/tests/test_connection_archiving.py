import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.main import create_app
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


@asynccontextmanager
async def authenticated_client(app, username: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": username, "password": "Archive-test-password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = client.cookies["ctm_csrf"]
        yield client


@pytest.mark.asyncio
async def test_archiving_idle_connection_clears_secrets_and_grants_but_keeps_history():
    store = MemoryStore()
    admin = store.add_user("admin", "admin@example.test", "hash", role="ADMIN")
    grantee = store.add_user("member", "member@example.test", "hash")
    connection = store.create_connection(
        "Private Drive",
        "GOOGLE_DRIVE",
        admin.id,
        encrypted_credentials=b"encrypted-secret",
        is_global=False,
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    await store.agrant_connection(grantee.id, connection.id, admin.id)
    transfer = await store.acreate_transfer(
        admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = "COMPLETED"
    await store.asave_transfer(transfer)
    await store.aadd_transfer_event(
        transfer.id, level="INFO", event_type="COMPLETED", message="Copy completed"
    )

    archived = await store.aarchive_connection(connection.id, actor_user_id=admin.id)

    assert archived is not None
    assert archived.deleted_at is not None
    assert store.connections[connection.id].encrypted_credentials == b""
    assert (grantee.id, connection.id) not in store.connection_grants
    assert await store.aget_connection(connection.id) is None
    assert all(row.id != connection.id for row in await store.alist_connections())
    assert not await store.ahas_connection_access(grantee.id, connection.id)
    assert await store.aget_transfer(transfer.id) == transfer
    assert (await store.alist_transfer_events(transfer.id))[0]["message"] == "Copy completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["QUEUED", "VALIDATING", "RUNNING", "RETRYING", "CANCELLING"])
async def test_archiving_connection_with_active_transfer_is_rejected_without_mutation(status: str):
    store = MemoryStore()
    admin = store.add_user("admin", "admin@example.test", "hash", role="ADMIN")
    connection = store.create_connection(
        "Source", "MOCK", admin.id, encrypted_credentials=b"encrypted-secret"
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    transfer = await store.acreate_transfer(
        admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = status
    await store.asave_transfer(transfer)

    with pytest.raises(ValueError, match="active transfer"):
        await store.aarchive_connection(connection.id, actor_user_id=admin.id)

    assert store.connections[connection.id].deleted_at is None
    assert store.connections[connection.id].encrypted_credentials == b"encrypted-secret"
    assert (await store.aget_transfer(transfer.id)).status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"])
async def test_terminal_transfer_does_not_block_archiving(status: str):
    store = MemoryStore()
    admin = store.add_user("admin", "admin@example.test", "hash", role="ADMIN")
    connection = store.create_connection("Source", "MOCK", admin.id)
    destination = store.create_connection("Destination", "MOCK", admin.id)
    transfer = await store.acreate_transfer(
        admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = status
    await store.asave_transfer(transfer)

    archived = await store.aarchive_connection(connection.id, actor_user_id=admin.id)

    assert archived is not None
    assert archived.deleted_at is not None
    assert await store.aget_transfer(transfer.id) is not None


@pytest.mark.asyncio
async def test_archived_connection_rejects_new_transfers_grants_and_stale_saves():
    store = MemoryStore()
    admin = store.add_user("admin", "admin@example.test", "hash", role="ADMIN")
    member = store.add_user("member", "member@example.test", "hash")
    connection = store.create_connection(
        "Source", "MOCK", admin.id, encrypted_credentials=b"encrypted-secret"
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    stale_record = replace(connection)
    await store.aarchive_connection(connection.id, actor_user_id=admin.id)

    with pytest.raises(ValueError) as transfer_error:
        await store.acreate_transfer(
            admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
        )
    assert getattr(transfer_error.value, "reason", None) == "not_found"

    with pytest.raises(ValueError):
        await store.agrant_connection(member.id, connection.id, admin.id)
    with pytest.raises(ValueError):
        await store.asave_connection(stale_record)

    assert store.connections[connection.id].encrypted_credentials == b""
    assert store.connections[connection.id].deleted_at is not None


@pytest.mark.asyncio
async def test_admin_can_archive_connection_with_csrf_and_audit_without_losing_history():
    store = MemoryStore()
    admin = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("Archive-test-password123!"),
        role="ADMIN",
    )
    member = store.add_user("member", "member@example.test", "hash")
    connection = store.create_connection(
        "Private Drive",
        "GOOGLE_DRIVE",
        admin.id,
        encrypted_credentials=b"ciphertext",
        is_global=False,
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    await store.agrant_connection(member.id, connection.id, admin.id)
    transfer = await store.acreate_transfer(
        admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = "COMPLETED"
    await store.asave_transfer(transfer)
    await store.aadd_transfer_event(
        transfer.id, level="INFO", event_type="COMPLETED", message="Copy completed"
    )
    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)

    async with authenticated_client(app, admin.username) as client:
        response = await client.delete(
            f"/api/v1/connections/{connection.id}", headers=client.headers
        )

        assert response.status_code == 204, response.text
        assert response.text == ""
        assert "ciphertext" not in response.text
        assert connection.deleted_at is not None
        assert connection.encrypted_credentials == b""
        assert (await store.aget_transfer(transfer.id)).source_connection_id == connection.id
        assert (await store.alist_transfer_events(transfer.id))[0]["message"] == "Copy completed"
        assert (member.id, connection.id) not in store.connection_grants
        assert all(
            item["id"] != str(connection.id)
            for item in (await client.get("/api/v1/connections")).json()["items"]
        )
        audit = next(
            event
            for event in await store.alist_audit_events()
            if event["action"] == "CONNECTION_ARCHIVED"
        )
        assert audit["resource_id"] == str(connection.id)
        assert audit["metadata"] == {"provider_type": "GOOGLE_DRIVE", "is_global": False}
        assert "ciphertext" not in str(audit)
        repeated = await client.delete(
            f"/api/v1/connections/{connection.id}", headers=client.headers
        )
        assert repeated.status_code == 404


@pytest.mark.asyncio
async def test_archive_requires_admin_and_csrf():
    store = MemoryStore()
    admin = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("Archive-test-password123!"),
        role="ADMIN",
    )
    member = store.add_user(
        "member",
        "member@example.test",
        PasswordService().hash("Archive-test-password123!"),
    )
    connection = store.create_connection("Shared", "MOCK", admin.id)
    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)

    async with authenticated_client(app, admin.username) as admin_client:
        csrf_token = admin_client.headers.pop("X-CSRF-Token")
        missing_csrf = await admin_client.delete(f"/api/v1/connections/{connection.id}")
        admin_client.headers["X-CSRF-Token"] = csrf_token
        assert missing_csrf.status_code == 403
        assert connection.deleted_at is None

    async with authenticated_client(app, member.username) as member_client:
        denied = await member_client.delete(
            f"/api/v1/connections/{connection.id}", headers=member_client.headers
        )
        assert denied.status_code == 403
        assert connection.deleted_at is None


@pytest.mark.asyncio
async def test_archive_api_returns_not_found_or_conflict_without_mutation():
    store = MemoryStore()
    admin = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("Archive-test-password123!"),
        role="ADMIN",
    )
    connection = store.create_connection(
        "Busy source", "MOCK", admin.id, encrypted_credentials=b"ciphertext"
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    transfer = await store.acreate_transfer(
        admin.id, connection.id, destination.id, ["/report.pdf"], "/archive"
    )
    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)

    async with authenticated_client(app, admin.username) as client:
        missing = await client.delete(f"/api/v1/connections/{uuid.uuid4()}", headers=client.headers)
        busy = await client.delete(f"/api/v1/connections/{connection.id}", headers=client.headers)

    assert missing.status_code == 404
    assert busy.status_code == 409
    assert connection.deleted_at is None
    assert connection.encrypted_credentials == b"ciphertext"
    assert (await store.aget_transfer(transfer.id)).status == "QUEUED"
    assert not any(event["action"] == "CONNECTION_ARCHIVED" for event in store.audit_events)


@pytest.mark.asyncio
async def test_archived_connection_cannot_be_used_by_connection_routes(tmp_path: Path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    admin = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("Archive-test-password123!"),
        role="ADMIN",
    )
    member = store.add_user("member", "member@example.test", "hash")
    connection_id = uuid.uuid4()
    encrypted = CredentialCipher.from_secret_file(str(master_key_file)).encrypt(
        connection_id, {"client_id": "client-id", "client_secret": "client-secret"}
    )
    connection = store.create_connection(
        "Google", "GOOGLE_DRIVE", admin.id, connection_id, encrypted, is_global=False
    )
    destination = store.create_connection("Destination", "MOCK", admin.id)
    settings = Settings(
        app_secret_key="test-secret",
        app_base_url="http://test",
        session_cookie_secure=False,
        master_encryption_key_file=str(master_key_file),
    )
    app = create_app(settings, store)

    async with authenticated_client(app, admin.username) as client:
        oauth_start = await client.post(
            f"/api/v1/connections/{connection.id}/google-oauth/start",
            headers=client.headers,
        )
        assert oauth_start.status_code == 200
        state = parse_qs(urlsplit(oauth_start.json()["authorization_url"]).query)["state"][0]
        archived = await client.delete(
            f"/api/v1/connections/{connection.id}", headers=client.headers
        )
        assert archived.status_code == 204

        item_listing = await client.get(f"/api/v1/connections/{connection.id}/items")
        connection_test = await client.post(
            f"/api/v1/connections/{connection.id}/test", headers=client.headers
        )
        oauth_again = await client.post(
            f"/api/v1/connections/{connection.id}/google-oauth/start",
            headers=client.headers,
        )
        oauth_callback = await client.get(
            "/api/v1/connections/google-oauth/callback",
            params={"state": state, "code": "authorization-code"},
        )
        update = await client.patch(
            f"/api/v1/connections/{connection.id}",
            json={"name": "Renamed"},
            headers=client.headers,
        )
        grant = await client.post(
            f"/api/v1/connections/{connection.id}/users/{member.id}",
            headers=client.headers,
        )
        transfer = await client.post(
            "/api/v1/transfers",
            json={
                "source_connection_id": str(connection.id),
                "destination_connection_id": str(destination.id),
                "source_paths": ["/Proyectos/informe.pdf"],
                "destination_path": "/Backup",
            },
            headers=client.headers,
        )

    assert [
        item.status_code
        for item in (
            item_listing,
            connection_test,
            oauth_again,
            oauth_callback,
            update,
            grant,
            transfer,
        )
    ] == [404, 404, 404, 404, 404, 404, 404]
