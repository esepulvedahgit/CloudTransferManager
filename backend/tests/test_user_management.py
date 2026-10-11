import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


async def authenticated_client(role: str = "ADMIN", must_change_password: bool = False):
    store = MemoryStore()
    user = store.add_user(
        username="admin",
        email="admin@example.test",
        password_hash=PasswordService().hash("Original-password123!"),
        role=role,
        must_change_password=must_change_password,
    )
    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    login = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "admin", "password": "Original-password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert login.status_code == 200
    return client, store, user


@pytest.mark.asyncio
async def test_admin_can_create_list_and_deactivate_user_revoking_session():
    client, store, _admin = await authenticated_client()
    csrf = client.cookies["ctm_csrf"]
    headers = {"X-CSRF-Token": csrf}
    created = await client.post(
        "/api/v1/users",
        json={
            "username": "operator",
            "email": "operator@example.test",
            "password": "Temporary-password123!",
            "role": "USER",
            "must_change_password": True,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert "password" not in created.json()
    assert created.json()["must_change_password"] is True
    assert (await client.get("/api/v1/users")).json()["total_count"] == 2

    target_id = created.json()["id"]
    target = store.find_user("operator")
    assert target is not None
    target_app = create_app(
        Settings(app_secret_key="test-secret", session_cookie_secure=False), store
    )
    target_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target_app), base_url="http://test"
    )
    login_csrf = (await target_client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    login = await target_client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "operator", "password": "Temporary-password123!"},
        headers={"X-CSRF-Token": login_csrf},
    )
    assert login.status_code == 200
    assert (await target_client.get("/api/v1/auth/me")).status_code == 200

    deactivated = await client.post(f"/api/v1/users/{target_id}/deactivate", headers=headers)
    assert deactivated.status_code == 200
    assert target.is_active is False
    assert (await target_client.get("/api/v1/auth/me")).status_code == 401
    await target_client.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_users_and_user_can_change_own_password():
    client, store, user = await authenticated_client("USER")
    csrf = client.cookies["ctm_csrf"]
    denied = await client.get("/api/v1/users")
    assert denied.status_code == 403
    changed = await client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "Original-password123!",
            "new_password": "New-password456!",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200, changed.text
    assert PasswordService().verify("New-password456!", user.password_hash)
    assert not user.must_change_password
    await client.aclose()


@pytest.mark.asyncio
async def test_temporary_password_blocks_api_until_password_is_changed():
    client, _store, user = await authenticated_client("USER", must_change_password=True)

    assert (await client.get("/api/v1/auth/me")).json()["must_change_password"] is True
    blocked = await client.get("/api/v1/connections")
    assert blocked.status_code == 403

    changed = await client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "Original-password123!",
            "new_password": "Changed-password456!",
        },
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    assert changed.status_code == 200, changed.text
    assert not user.must_change_password
    assert (await client.get("/api/v1/connections")).status_code == 200
    await client.aclose()


@pytest.mark.asyncio
async def test_users_only_browse_global_or_assigned_connections():
    admin_client, store, admin = await authenticated_client()
    member = store.add_user(
        username="member",
        email="member@example.test",
        password_hash=PasswordService().hash("Member-password123!"),
    )
    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)
    private = await admin_client.post(
        "/api/v1/connections",
        json={"name": "Private", "provider_type": "MOCK", "is_global": False},
        headers={"X-CSRF-Token": admin_client.cookies["ctm_csrf"]},
    )
    assert private.status_code == 201, private.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as member_client:
        csrf = (await member_client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await member_client.post(
            "/api/v1/auth/login",
            json={"username_or_email": member.username, "password": "Member-password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        private_id = private.json()["id"]
        assert (await member_client.get("/api/v1/connections")).json()["items"] == []
        assert (
            await member_client.get(f"/api/v1/connections/{private_id}/items")
        ).status_code == 404

        granted = await admin_client.post(
            f"/api/v1/connections/{private_id}/users/{member.id}",
            headers={"X-CSRF-Token": admin_client.cookies["ctm_csrf"]},
        )
        assert granted.status_code == 200, granted.text
        assert len((await member_client.get("/api/v1/connections")).json()["items"]) == 1
        assert (
            await member_client.get(f"/api/v1/connections/{private_id}/items?path=/Proyectos")
        ).status_code == 200
    await admin_client.aclose()


@pytest.mark.asyncio
async def test_admin_can_edit_and_deactivate_connection_without_removing_it():
    client, store, admin = await authenticated_client()
    connection = store.create_connection("Old name", "MOCK", admin.id)
    headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
    updated = await client.patch(
        f"/api/v1/connections/{connection.id}",
        json={"name": "Renamed", "is_global": False},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["is_global"] is False
    deactivated = await client.post(
        f"/api/v1/connections/{connection.id}/deactivate", headers=headers
    )
    assert deactivated.status_code == 200
    assert store.get_connection(connection.id).status == "INACTIVE"
    await client.aclose()


@pytest.mark.asyncio
async def test_admin_can_reactivate_connection_after_successful_test():
    client, store, admin = await authenticated_client()
    connection = store.create_connection("Simulada", "MOCK", admin.id)
    headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
    await client.post(f"/api/v1/connections/{connection.id}/deactivate", headers=headers)

    tested = await client.post(f"/api/v1/connections/{connection.id}/test", headers=headers)

    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is True
    assert connection.status == "ACTIVE"
    assert connection.last_test_status == "SUCCESS"
    assert connection.last_tested_at is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_admin_can_update_user_role_and_email_without_exposing_password_hash():
    client, store, _admin = await authenticated_client()
    target = store.add_user(
        username="member",
        email="member@example.test",
        password_hash=PasswordService().hash("Member-password123!"),
    )
    updated = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": "MEMBER2@example.test", "role": "ADMIN"},
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "member2@example.test"
    assert updated.json()["role"] == "ADMIN"
    assert "password_hash" not in updated.json()
    await client.aclose()
