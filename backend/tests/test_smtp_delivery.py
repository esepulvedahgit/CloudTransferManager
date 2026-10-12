import uuid
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

import app.services.notifications as notifications
import app.services.smtp_config as smtp_config_module
from app.security.credentials import CredentialCipher
from app.services.memory_store import MemoryStore, SMTPSettingsRecord
from app.settings import Settings


async def resolve_config(store, settings):
    resolver = getattr(smtp_config_module, "load_smtp_server_config", None)
    assert callable(resolver), "load_smtp_server_config must resolve saved and environment SMTP"
    return await resolver(store, settings)


@pytest.mark.asyncio
async def test_saved_smtp_config_takes_precedence_over_environment(tmp_path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    settings = Settings(
        master_encryption_key_file=str(master_key_file),
        smtp_host="environment.smtp.test",
        smtp_port=587,
        smtp_username="environment-user",
        smtp_from_address="environment@example.test",
    )
    store = MemoryStore()
    settings_id = uuid.uuid4()
    record = SMTPSettingsRecord(
        id=settings_id,
        host="database.smtp.test",
        port=2525,
        use_tls=False,
        from_address="database@example.test",
        encrypted_credentials=CredentialCipher(b"m" * 32).encrypt(
            settings_id, {"username": "database-user", "password": "database-password"}
        ),
    )
    await store.asave_smtp_settings(record)

    resolved = await resolve_config(store, settings)

    assert resolved.host == "database.smtp.test"
    assert resolved.port == 2525
    assert resolved.username == "database-user"
    assert resolved.password == "database-password"
    assert resolved.from_address == "database@example.test"
    assert resolved.use_tls is False


@pytest.mark.asyncio
async def test_smtp_config_falls_back_to_environment_and_password_file(tmp_path):
    master_key_file = tmp_path / "master_key"
    master_key_file.write_bytes(b"m" * 32)
    password_file = tmp_path / "smtp_password"
    password_file.write_text("environment-password\n", encoding="utf-8")
    settings = Settings(
        master_encryption_key_file=str(master_key_file),
        smtp_host="environment.smtp.test",
        smtp_port=2526,
        smtp_username="environment-user",
        smtp_password_file=str(password_file),
        smtp_from_address="environment@example.test",
        smtp_use_tls=False,
    )

    resolved = await resolve_config(MemoryStore(), settings)

    assert resolved.host == "environment.smtp.test"
    assert resolved.port == 2526
    assert resolved.username == "environment-user"
    assert resolved.password == "environment-password"
    assert resolved.from_address == "environment@example.test"
    assert resolved.use_tls is False


def test_smtp_delivery_uses_starttls_and_auth(monkeypatch):
    events = []
    connections = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            connections.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            events.append("quit")

        def starttls(self, context):
            events.append(("starttls", context))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message: EmailMessage):
            events.append(("send", message["From"], message["To"]))

    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    smtp = smtp_config_module.SMTPServerConfig(
        host="database.smtp.test",
        port=2525,
        username="database-user",
        password="database-password",
        from_address="database@example.test",
        use_tls=True,
    )
    transfer = SimpleNamespace(
        public_id="TRF-2026-000001",
        status="COMPLETED",
        bytes_transferred=1024,
        created_at=None,
        completed_at=None,
    )

    notifications.send_transfer_email(
        Settings(), smtp, "recipient@example.test", transfer, "MEGA", "Drive"
    )

    assert connections == [("database.smtp.test", 2525, 20)]
    event_names = [event if isinstance(event, str) else event[0] for event in events]
    assert event_names.index("starttls") < event_names.index("login") < event_names.index("send")
    assert ("login", "database-user", "database-password") in events
    assert ("send", "database@example.test", "recipient@example.test") in events
    assert event_names[-1] == "quit"
