import httpx
import pytest

from app.main import create_app
from app.providers.mock import MockStorageProvider
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


def build_client_app(role: str = "USER"):
    store = MemoryStore()
    user = store.add_user(
        username="reader",
        email="reader@example.test",
        password_hash=PasswordService().hash("A-Strong-Password123!"),
        role=role,
    )
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    return app, store, user


@pytest.mark.asyncio
async def test_login_uses_secure_http_only_session_cookie():
    app, _store, _user = build_client_app("USER")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        response = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "reader", "password": "A-Strong-Password123!"},
            headers={"X-CSRF-Token": csrf},
        )

    cookie = response.headers["set-cookie"].lower()
    assert "ctm_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


@pytest.mark.asyncio
async def test_five_invalid_logins_lock_user_for_fifteen_minutes():
    app, store, user = build_client_app("USER")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        for _ in range(5):
            response = await client.post(
                "/api/v1/auth/login",
                json={"username_or_email": "reader", "password": "wrong"},
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 401
            assert "no fue posible iniciar sesión" in response.json()["detail"].lower()

    assert user.failed_login_attempts == 5
    assert user.locked_until is not None


@pytest.mark.asyncio
async def test_user_role_cannot_create_global_connection():
    app, store, user = build_client_app("USER")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "reader", "password": "A-Strong-Password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        csrf = client.cookies["ctm_csrf"]
        response = await client.post(
            "/api/v1/connections",
            json={"name": "No autorizada", "provider_type": "MOCK"},
            headers={"X-CSRF-Token": csrf},
        )

    assert user.role == "USER"
    assert response.status_code == 403
    assert store.list_connections() == []


@pytest.mark.asyncio
async def test_explorer_rejects_path_traversal():
    app, store, _user = build_client_app("ADMIN")
    connection = store.create_connection("Mock", "MOCK", next(iter(store.users)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "reader", "password": "A-Strong-Password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        response = await client.get(
            f"/api/v1/connections/{connection.id}/items?path=../../etc/passwd"
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_transfer_request_is_queued_without_checking_remote_source(monkeypatch):
    store = MemoryStore()
    user = store.add_user(
        username="operator",
        email="operator@example.test",
        password_hash=PasswordService().hash("A-Strong-Password123!"),
        role="ADMIN",
    )
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    monkeypatch.setattr("app.worker.run_transfer.delay", lambda _transfer_id: None)

    async def remote_check_must_not_run(self, path):
        del self, path
        raise AssertionError("the HTTP request must not inspect remote storage")

    monkeypatch.setattr(MockStorageProvider, "get_item", remote_check_must_not_run)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "operator", "password": "A-Strong-Password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        response = await client.post(
            "/api/v1/transfers",
            json={
                "source_connection_id": str(source.id),
                "destination_connection_id": str(destination.id),
                "source_paths": ["/some/remote/file.bin"],
                "destination_path": "/backup",
            },
            headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
        )
        too_many_paths = await client.post(
            "/api/v1/transfers",
            json={
                "source_connection_id": str(source.id),
                "destination_connection_id": str(destination.id),
                "source_paths": [f"/item-{index}" for index in range(101)],
                "destination_path": "/backup",
            },
            headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
        )

    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"
    assert too_many_paths.status_code == 422
    assert "100 rutas" in too_many_paths.json()["detail"][0]["msg"]
