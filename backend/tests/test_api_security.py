import httpx
import pytest

from app.main import create_app
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
