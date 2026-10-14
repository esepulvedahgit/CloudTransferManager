from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


def _store() -> MemoryStore:
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    return store


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "admin", "password": "SafePassword123!"},
        headers={"X-CSRF-Token": csrf},
    )
    return {"X-CSRF-Token": client.cookies["ctm_csrf"]}


def _payload() -> dict[str, object]:
    return {
        "name": "Nextcloud de prueba",
        "provider_type": "NEXTCLOUD",
        "credentials": {
            "url": "https://cloud.example.test",
            "username": "alice",
            "password": "application-secret",
        },
    }


@pytest.mark.asyncio
async def test_nextcloud_is_advertised_and_creatable_without_environment_flags(tmp_path: Path):
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"m" * 32)
    app = create_app(
        Settings(
            app_secret_key="test-secret",
            session_cookie_secure=False,
            master_encryption_key_file=str(key_file),
        ),
        store=_store(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await _login(client)
        capabilities = await client.get("/api/v1/system/capabilities")
        response = await client.post("/api/v1/connections", json=_payload(), headers=headers)

    assert "NEXTCLOUD" in capabilities.json()["enabled_providers"]
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "INACTIVE"


@pytest.mark.asyncio
async def test_nextcloud_is_advertised_without_environment_origin_allowlist():
    app = create_app(
        Settings(
            app_secret_key="test-secret",
            session_cookie_secure=False,
        ),
        store=_store(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _login(client)
        response = await client.get("/api/v1/system/capabilities")

    assert "NEXTCLOUD" in response.json()["enabled_providers"]


@pytest.mark.asyncio
async def test_nextcloud_connection_secrets_are_encrypted_and_not_returned(
    tmp_path: Path,
):
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"m" * 32)
    store = _store()
    app = create_app(
        Settings(
            app_secret_key="test-secret",
            session_cookie_secure=False,
            master_encryption_key_file=str(key_file),
        ),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await _login(client)
        capabilities = await client.get("/api/v1/system/capabilities")
        response = await client.post("/api/v1/connections", json=_payload(), headers=headers)

    assert "NEXTCLOUD" in capabilities.json()["enabled_providers"]
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "INACTIVE"
    assert response.json()["credentials_configured"] is True
    assert "application-secret" not in response.text
    connection = next(iter(store.connections.values()))
    assert b"application-secret" not in connection.encrypted_credentials
    assert CredentialCipher.from_secret_file(key_file).decrypt(
        connection.id, connection.encrypted_credentials
    )["password"] == "application-secret"


@pytest.mark.asyncio
async def test_nextcloud_connection_accepts_admin_supplied_domain_without_allowlist(
    tmp_path: Path,
):
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"m" * 32)
    app = create_app(
        Settings(
            app_secret_key="test-secret",
            session_cookie_secure=False,
            master_encryption_key_file=str(key_file),
        ),
        store=_store(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await _login(client)
        response = await client.post("/api/v1/connections", json=_payload(), headers=headers)

    assert response.status_code == 201, response.text
    assert "application-secret" not in response.text
