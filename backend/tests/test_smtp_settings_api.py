import asyncio
import smtplib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import app.main as main
import app.services.smtp_config as smtp_config_module
from app.main import create_app
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings

USERNAME = "smtp-account@example.test"
PASSWORD = "smtp-secret-value"


async def authenticated_client(
    tmp_path: Path, role: str = "ADMIN", settings_overrides: dict | None = None
):
    store = MemoryStore()
    store.add_user(
        username="operator",
        email="operator@example.test",
        password_hash=PasswordService().hash("Original-password123!"),
        role=role,
    )
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"k" * 32)
    settings_values = {
        "app_secret_key": "test-secret",
        "session_cookie_secure": False,
        "master_encryption_key_file": str(key_file),
    }
    settings_values.update(settings_overrides or {})
    settings = Settings(**settings_values)
    app = create_app(settings, store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    login = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": "operator", "password": "Original-password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["ctm_csrf"]
    return client, store, settings


def smtp_payload(**overrides):
    return {
        "host": "smtp.example.test",
        "port": 587,
        "use_tls": True,
        "from_address": "notifications@example.test",
        "username": USERNAME,
        "password": PASSWORD,
        **overrides,
    }


@pytest.mark.asyncio
async def test_smtp_settings_routes_require_admin(tmp_path):
    client, _store, _settings = await authenticated_client(tmp_path, "USER")
    try:
        assert (await client.get("/api/v1/settings/smtp")).status_code == 403
        assert (await client.put("/api/v1/settings/smtp", json=smtp_payload())).status_code == 403
        assert (
            await client.post("/api/v1/settings/smtp/test", json=smtp_payload())
        ).status_code == 403
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_settings_response_never_returns_credentials(tmp_path):
    client, _store, _settings = await authenticated_client(tmp_path)
    try:
        saved = await client.put("/api/v1/settings/smtp", json=smtp_payload())
        assert saved.status_code == 200, saved.text
        loaded = await client.get("/api/v1/settings/smtp")
        assert loaded.status_code == 200
        assert loaded.json() == {
            "host": "smtp.example.test",
            "port": 587,
            "use_tls": True,
            "from_address": "notifications@example.test",
            "credentials_configured": True,
        }
        assert USERNAME not in loaded.text
        assert PASSWORD not in loaded.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_update_encrypts_and_preserves_omitted_credentials(tmp_path):
    client, store, settings = await authenticated_client(tmp_path)
    try:
        created = await client.put("/api/v1/settings/smtp", json=smtp_payload())
        assert created.status_code == 200, created.text
        first_record = await store.aget_smtp_settings()
        assert first_record is not None
        assert USERNAME.encode() not in first_record.encrypted_credentials
        assert PASSWORD.encode() not in first_record.encrypted_credentials
        assert CredentialCipher.from_secret_file(settings.master_encryption_key_file).decrypt(
            first_record.id, first_record.encrypted_credentials
        ) == {"username": USERNAME, "password": PASSWORD}

        updated = await client.put(
            "/api/v1/settings/smtp",
            json=smtp_payload(
                host="mail.example.test", username="", password="", port=465, use_tls=False
            ),
        )
        assert updated.status_code == 200, updated.text
        second_record = await store.aget_smtp_settings()
        assert second_record is not None
        assert second_record.id == first_record.id
        assert second_record.host == "mail.example.test"
        assert second_record.port == 465
        assert second_record.encrypted_credentials == first_record.encrypted_credentials
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_test_uses_draft_without_persisting(tmp_path, monkeypatch):
    client, store, _settings = await authenticated_client(tmp_path)
    captured = []
    monkeypatch.setattr(
        main, "test_smtp_connection", lambda config: captured.append(config), raising=False
    )
    try:
        await client.put("/api/v1/settings/smtp", json=smtp_payload())
        saved = await store.aget_smtp_settings()
        draft = smtp_payload(
            host="draft.example.test", port=2525, username="draft-user", password="draft-pass"
        )
        response = await client.post("/api/v1/settings/smtp/test", json=draft)

        assert response.status_code == 200, response.text
        assert response.json() == {"success": True, "message": "Conexión SMTP correcta"}
        assert len(captured) == 1
        assert captured[0].host == "draft.example.test"
        assert captured[0].port == 2525
        assert captured[0].username == "draft-user"
        assert captured[0].password == "draft-pass"
        assert await store.aget_smtp_settings() == saved
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_get_and_test_fall_back_to_environment_and_password_file(tmp_path, monkeypatch):
    password_file = tmp_path / "smtp_password"
    password_file.write_text("environment-password\n", encoding="utf-8")
    client, store, _settings = await authenticated_client(
        tmp_path,
        settings_overrides={
            "smtp_host": "env.smtp.example.test",
            "smtp_port": 2526,
            "smtp_username": "environment-user",
            "smtp_password_file": str(password_file),
            "smtp_use_tls": False,
            "smtp_from_address": "env@example.test",
        },
    )
    captured = []
    monkeypatch.setattr(main, "test_smtp_connection", lambda config: captured.append(config))
    try:
        loaded = await client.get("/api/v1/settings/smtp")
        assert loaded.status_code == 200
        assert loaded.json() == {
            "host": "env.smtp.example.test",
            "port": 2526,
            "use_tls": False,
            "from_address": "env@example.test",
            "credentials_configured": True,
        }
        assert "environment-user" not in loaded.text
        draft = await client.post(
            "/api/v1/settings/smtp/test",
            json=smtp_payload(username="", password=""),
        )
        assert draft.status_code == 200
        assert captured[0].username == "environment-user"
        assert captured[0].password == "environment-password"
        assert await store.aget_smtp_settings() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_settings_audit_excludes_credentials(tmp_path):
    client, store, _settings = await authenticated_client(tmp_path)
    try:
        response = await client.put("/api/v1/settings/smtp", json=smtp_payload())
        assert response.status_code == 200, response.text
        events = [
            event for event in store.audit_events if event["action"] == "SMTP_SETTINGS_UPDATED"
        ]
        assert len(events) == 1
        assert USERNAME not in str(events[0])
        assert PASSWORD not in str(events[0])
        assert events[0]["metadata"] == {
            "changed_fields": ["host", "port", "use_tls", "from_address"]
        }
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "smtp.example.test\r\nInjected: true"},
        {"port": 0},
        {"port": 65536},
        {"from_address": "not-an-email"},
        {"username": "partial-user", "password": ""},
    ],
)
async def test_smtp_update_rejects_invalid_fields(tmp_path, overrides):
    client, store, _settings = await authenticated_client(tmp_path)
    try:
        response = await client.put("/api/v1/settings/smtp", json=smtp_payload(**overrides))
        assert response.status_code == 422
        assert PASSWORD not in response.text
        assert await store.aget_smtp_settings() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "overrides"),
    [
        ("put", {"username": "", "password": "password-only-secret"}),
        ("put", {"username": "smtp-user", "password": "line\nsecret-value"}),
        ("put", {"username": "smtp-user", "password": "oversized-secret-" + "x" * 1024}),
        ("test", {"username": "", "password": "test-password-secret"}),
        ("test", {"username": "smtp-user", "password": "test\nsecret-value"}),
    ],
)
async def test_smtp_validation_errors_never_echo_credentials(tmp_path, path, overrides):
    client, store, _settings = await authenticated_client(tmp_path)
    try:
        if path == "put":
            response = await client.put(
                "/api/v1/settings/smtp", json=smtp_payload(**overrides)
            )
        else:
            response = await client.post(
                "/api/v1/settings/smtp/test", json=smtp_payload(**overrides)
            )

        assert response.status_code == 422
        assert "secret" not in response.text
        assert "smtp-user" not in response.text
        assert await store.aget_smtp_settings() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "[smtp.example.test]"},
        {"from_address": "sender@example..test"},
        {"from_address": "sender@example.test\x00hidden"},
    ],
)
async def test_smtp_rejects_malformed_host_and_sender(tmp_path, overrides):
    client, store, _settings = await authenticated_client(tmp_path)
    try:
        response = await client.put(
            "/api/v1/settings/smtp", json=smtp_payload(**overrides)
        )
        assert response.status_code == 422
        assert await store.aget_smtp_settings() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_normalizes_bracketed_ipv6_host(tmp_path):
    client, _store, _settings = await authenticated_client(tmp_path)
    try:
        response = await client.put(
            "/api/v1/settings/smtp", json=smtp_payload(host="[::1]")
        )
        assert response.status_code == 200, response.text
        assert response.json()["host"] == "::1"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["put", "test"])
@pytest.mark.parametrize("host", ["fe80::1%\x00", "[fe80::1%\x00]"])
async def test_smtp_rejects_control_characters_in_ipv6_scope(
    tmp_path, monkeypatch, path, host
):
    client, store, _settings = await authenticated_client(tmp_path)
    connection_attempts = []
    monkeypatch.setattr(
        main,
        "test_smtp_connection",
        lambda config: connection_attempts.append(config),
    )
    try:
        if path == "put":
            response = await client.put(
                "/api/v1/settings/smtp", json=smtp_payload(host=host)
            )
        else:
            response = await client.post(
                "/api/v1/settings/smtp/test", json=smtp_payload(host=host)
            )

        assert response.status_code == 422
        assert await store.aget_smtp_settings() is None
        assert connection_attempts == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_smtp_partial_update_preserves_existing_ciphertext(tmp_path):
    client, store, _settings = await authenticated_client(tmp_path)
    try:
        created = await client.put("/api/v1/settings/smtp", json=smtp_payload())
        assert created.status_code == 200, created.text
        saved_before = await store.aget_smtp_settings()
        assert saved_before is not None

        rejected = await client.put(
            "/api/v1/settings/smtp",
            json=smtp_payload(username="smtp-user", password=""),
        )

        assert rejected.status_code == 422
        assert PASSWORD not in rejected.text
        assert await store.aget_smtp_settings() == saved_before
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_first_smtp_saves_encrypt_with_persisted_record_id(tmp_path):
    class FirstSaveRaceStore(MemoryStore):
        def __init__(self):
            super().__init__()
            self.initial_reads = 0
            self.both_reads_finished = asyncio.Event()

        async def aget_smtp_settings(self):
            snapshot = self.smtp_settings
            self.initial_reads += 1
            if self.initial_reads == 2:
                self.both_reads_finished.set()
            await self.both_reads_finished.wait()
            return snapshot

        async def acreate_smtp_settings_if_absent(self, settings):
            if self.smtp_settings is None:
                self.smtp_settings = settings
            return self.smtp_settings

        async def asave_smtp_settings(self, settings):
            if self.smtp_settings is not None:
                settings = replace(settings, id=self.smtp_settings.id)
            self.smtp_settings = settings
            return settings

    store = FirstSaveRaceStore()
    store.add_user(
        username="operator",
        email="operator@example.test",
        password_hash=PasswordService().hash("Original-password123!"),
        role="ADMIN",
    )
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"k" * 32)
    settings = Settings(
        app_secret_key="test-secret",
        session_cookie_secure=False,
        master_encryption_key_file=str(key_file),
    )
    app = create_app(settings, store)
    clients = []
    for _ in range(2):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "operator", "password": "Original-password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        client.headers["X-CSRF-Token"] = client.cookies["ctm_csrf"]
        clients.append(client)
    try:
        responses = await asyncio.gather(
            *[
                client.put(
                    "/api/v1/settings/smtp",
                    json=smtp_payload(
                        host=f"smtp{index}.example.test",
                        username=f"smtp-user-{index}",
                        password=f"unique-password-{index}",
                    ),
                )
                for index, client in enumerate(clients)
            ]
        )
        assert all(response.status_code == 200 for response in responses)
        persisted = await store.aget_smtp_settings()
        assert persisted is not None
        credentials = CredentialCipher.from_secret_file(
            settings.master_encryption_key_file
        ).decrypt(persisted.id, persisted.encrypted_credentials)
        assert credentials["username"].startswith("smtp-user-")
        audit_ids = [
            event["resource_id"]
            for event in store.audit_events
            if event["action"] == "SMTP_SETTINGS_UPDATED"
        ]
        assert audit_ids == [str(persisted.id), str(persisted.id)]
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_smtp_test_returns_generic_failure(tmp_path, monkeypatch):
    client, store, _settings = await authenticated_client(tmp_path)

    class FailingSMTP:
        def __init__(self, *_args, **_kwargs):
            raise smtplib.SMTPAuthenticationError(
                535, f"authentication rejected {PASSWORD}".encode()
            )

    monkeypatch.setattr(smtp_config_module.smtplib, "SMTP", FailingSMTP)
    try:
        response = await client.post("/api/v1/settings/smtp/test", json=smtp_payload())
        assert response.status_code == 200
        assert response.json() == {"success": False, "message": "No se pudo conectar con SMTP"}
        assert USERNAME not in response.text
        assert PASSWORD not in response.text
        assert await store.aget_smtp_settings() is None
    finally:
        await client.aclose()
