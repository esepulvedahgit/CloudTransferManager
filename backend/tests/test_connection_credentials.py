import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app.main as main
from app.main import create_app
from app.providers.base import ConnectionTestResult
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


@pytest.mark.asyncio
async def test_connection_credentials_are_encrypted_and_never_returned(tmp_path: Path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    app = create_app(
        Settings(
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
            master_encryption_key_file=str(master_key_file),
        ),
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
        response = await client.post(
            "/api/v1/connections",
            json={
                "name": "MEGA privado",
                "provider_type": "MEGA",
                "credentials": {"username": "mega@example.test", "password": "mega-secret"},
            },
            headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["credentials_configured"] is True
    assert response.json()["status"] == "INACTIVE"
    assert "mega-secret" not in response.text
    connection = next(iter(store.connections.values()))
    assert b"mega-secret" not in connection.encrypted_credentials


@pytest.mark.asyncio
async def test_google_oauth_connection_can_be_staged_without_a_refresh_token(tmp_path: Path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    app = create_app(
        Settings(
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
            master_encryption_key_file=str(master_key_file),
        ),
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        response = await client.post(
            "/api/v1/connections",
            json={
                "name": "Google pendiente",
                "provider_type": "GOOGLE_DRIVE",
                "credentials": {"client_id": "client-id", "client_secret": "client-secret"},
            },
            headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
        )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "INACTIVE"
    assert response.json()["credentials_configured"] is True


@pytest.mark.asyncio
async def test_google_oauth_callback_exchanges_and_encrypts_tokens(monkeypatch, tmp_path: Path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    app = create_app(
        Settings(
            app_secret_key="test-secret-key",
            app_base_url="https://transfer.example.test",
            session_cookie_secure=False,
            master_encryption_key_file=str(master_key_file),
        ),
        store=store,
    )
    exchange: dict[str, object] = {}

    class FakeTokenResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            exchange["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, data):
            exchange["url"] = url
            exchange["data"] = data
            return FakeTokenResponse()

    test_client_factory = httpx.AsyncClient
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    async with test_client_factory(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
        created = await client.post(
            "/api/v1/connections",
            json={
                "name": "Google corporativo",
                "provider_type": "GOOGLE_DRIVE",
                "credentials": {
                    "client_id": "oauth-client-id",
                    "client_secret": "oauth-client-secret",
                    "team_drive": "shared-drive",
                },
            },
            headers=headers,
        )
        connection_id = created.json()["id"]
        started = await client.post(
            f"/api/v1/connections/{connection_id}/google-oauth/start", headers=headers
        )
        authorization = urlsplit(started.json()["authorization_url"])
        query = parse_qs(authorization.query)
        assert authorization.hostname == "accounts.google.com"
        assert query["access_type"] == ["offline"]
        assert query["scope"] == ["https://www.googleapis.com/auth/drive"]
        assert "oauth-client-secret" not in started.text

        callback = await client.get(
            "/api/v1/connections/google-oauth/callback",
            params={"state": query["state"][0], "code": "one-time-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303
    assert callback.headers["location"] == "https://transfer.example.test/?google_oauth=success"
    assert exchange["url"] == "https://oauth2.googleapis.com/token"
    exchange_data = exchange["data"]
    assert isinstance(exchange_data, dict)
    assert exchange_data["client_secret"] == "oauth-client-secret"
    assert exchange_data["code"] == "one-time-code"
    connection = store.connections[uuid.UUID(connection_id)]
    credentials = CredentialCipher(b"m" * 32).decrypt(
        connection.id, connection.encrypted_credentials
    )
    assert credentials["refresh_token"] == "refresh-token"
    assert credentials["access_token"] == "access-token"
    assert connection.status == "INACTIVE"


@pytest.mark.asyncio
async def test_seafile_api_connection_uses_transient_otp_and_persists_only_encrypted_token(
    monkeypatch, tmp_path: Path
):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    auth_calls: list[tuple[str, str, str | None]] = []

    class FakeAuthClient:
        def __init__(self, base_url: str):
            assert base_url == "https://seafile.example.test"

        async def obtain_token(self, username: str, password: str, otp: str | None = None) -> str:
            auth_calls.append((username, password, otp))
            return "seafile-account-token"

        async def ping(self, token: str) -> bool:
            return token == "seafile-account-token"

    class FakeApiProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def test_connection(self):
            return ConnectionTestResult(success=True, message="Conexión verificada exitosamente")

    monkeypatch.setattr(main, "SeafileAuthClient", FakeAuthClient, raising=False)
    monkeypatch.setattr(main, "SeafileApiProvider", FakeApiProvider, raising=False)

    app = create_app(
        Settings(
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
            master_encryption_key_file=str(master_key_file),
        ),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
        created = await client.post(
            "/api/v1/connections",
            json={
                "name": "Seafile API",
                "provider_type": "SEAFILE",
                "adapter_type": "SEAFILE_API",
                "credentials": {
                    "url": "https://seafile.example.test",
                    "username": "operator@example.test",
                    "password": "seafile-password",
                    "library": "Archive",
                    "library_password": "library-secret",
                },
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["adapter_type"] == "SEAFILE_API"
        tested = await client.post(
            f"/api/v1/connections/{created.json()['id']}/test",
            json={"otp": "654321"},
            headers=headers,
        )

    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is True
    assert auth_calls == [("operator@example.test", "seafile-password", "654321")]
    assert "654321" not in tested.text
    assert "seafile-account-token" not in tested.text
    connection = store.connections[next(iter(store.connections))]
    credentials = CredentialCipher(b"m" * 32).decrypt(
        connection.id, connection.encrypted_credentials
    )
    assert credentials["auth_token"] == "seafile-account-token"
    assert "otp" not in credentials
    assert "654321" not in repr(credentials)
    assert connection.adapter_type == "SEAFILE_API"
    assert connection.status == "ACTIVE"


@pytest.mark.asyncio
async def test_changing_seafile_adapter_requires_a_new_connection_test(tmp_path: Path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    store = MemoryStore()
    store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    app = create_app(
        Settings(
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
            master_encryption_key_file=str(master_key_file),
        ),
        store=store,
    )
    connection_id = uuid.uuid4()
    encrypted = CredentialCipher(b"m" * 32).encrypt(
        connection_id,
        {
            "url": "https://seafile.example.test",
            "username": "operator@example.test",
            "password": "seafile-password",
            "auth_token": "account-token",
        },
    )
    connection = store.create_connection(
        "Seafile",
        "SEAFILE",
        next(iter(store.users)),
        connection_id=connection_id,
        encrypted_credentials=encrypted,
        adapter_type="RCLONE",
    )
    connection.status = "ACTIVE"
    connection.last_test_status = "SUCCESS"
    connection.last_tested_at = datetime.now(UTC)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        response = await client.patch(
            f"/api/v1/connections/{connection_id}",
            json={"adapter_type": "SEAFILE_API"},
            headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["adapter_type"] == "SEAFILE_API"
    assert response.json()["status"] == "INACTIVE"
    assert response.json()["last_test_status"] is None
