import smtplib
import uuid
from contextlib import asynccontextmanager

import pytest

import app.services.notifications as notifications
import app.worker as worker
from app.security.credentials import CredentialCipher
from app.services.memory_store import MemoryStore, SMTPSettingsRecord
from app.services.notifications import NotificationDeliveryError
from app.services.smtp_config import SMTPServerConfig
from app.settings import Settings
from app.worker import celery_app


def test_transfer_and_notification_tasks_use_worker_queue():
    routes = celery_app.conf.task_routes

    assert routes["app.worker.run_transfer"]["queue"] == "transfers"
    assert routes["app.worker.send_transfer_notification"]["queue"] == "transfers"
    assert celery_app.conf.task_default_queue == "transfers"

    assert celery_app.conf.broker_transport_options["visibility_timeout"] >= 24 * 60 * 60


@pytest.mark.asyncio
async def test_redelivered_transfer_does_not_run_or_clear_active_cancellation(monkeypatch):
    transfer_id = uuid.uuid4()
    calls: list[str] = []

    class LockedStore:
        @asynccontextmanager
        async def transfer_execution_lock(self, supplied_id):
            calls.append(f"lock:{supplied_id}")
            yield False

        async def aclose(self):
            calls.append("store_closed")

    class EventBus:
        def __init__(self, _client):
            pass

        async def clear_cancel(self, _transfer_id):
            calls.append("cancel_cleared")

        async def aclose(self):
            calls.append("event_bus_closed")

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: LockedStore())
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "RedisEventBus", EventBus)
    monkeypatch.setattr(
        worker,
        "TransferExecutor",
        lambda *_args, **_kwargs: pytest.fail("a duplicate delivery must not execute the transfer"),
    )
    monkeypatch.setattr(
        worker,
        "enqueue_transfer_notification",
        lambda *_args, **_kwargs: pytest.fail("a duplicate delivery must not notify"),
    )

    await worker._execute_transfer(transfer_id)

    assert calls == [f"lock:{transfer_id}", "event_bus_closed", "store_closed"]


@pytest.mark.asyncio
async def test_worker_passes_resolved_smtp_config(monkeypatch, tmp_path):
    settings = Settings(
        app_secret_key="test-secret",
        master_encryption_key_file=str(tmp_path / "master_key"),
    )
    transfer_store = MemoryStore()
    user = transfer_store.add_user("operator", "operator@example.test", "hash")
    source = transfer_store.create_connection("Source", "MOCK", user.id)
    destination = transfer_store.create_connection("Destination", "MOCK", user.id)
    transfer = transfer_store.create_transfer(
        requested_by=user.id,
        source_connection_id=source.id,
        destination_connection_id=destination.id,
        source_paths=["/source"],
        destination_path="/destination",
        send_email_notification=True,
    )

    async def close_store():
        return None

    transfer_store.aclose = close_store
    resolved = SMTPServerConfig(
        host="database.smtp.test",
        port=2525,
        username="database-user",
        password="database-password",
        from_address="database@example.test",
        use_tls=True,
    )
    captured = {}

    async def load_config(store, supplied_settings):
        assert store is transfer_store
        assert supplied_settings is settings
        return resolved

    def capture_email(*args):
        captured["args"] = args

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: transfer_store)
    monkeypatch.setattr(worker, "load_smtp_server_config", load_config, raising=False)
    monkeypatch.setattr(worker, "send_transfer_email", capture_email)

    await worker._send_notification(transfer.id)

    assert captured["args"][0] is settings
    assert captured["args"][1] == resolved
    assert captured["args"][2] == user.email
    assert captured["args"][3] is transfer


@pytest.mark.asyncio
async def test_worker_uses_saved_smtp_and_keeps_transfer_state_on_delivery_failure(
    monkeypatch, tmp_path
):
    password = "saved-smtp-password"
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"m" * 32)
    settings = Settings(
        app_secret_key="test-secret",
        master_encryption_key_file=str(key_file),
        smtp_host="environment.smtp.test",
        smtp_username="environment-user",
        smtp_from_address="environment@example.test",
    )
    store = MemoryStore()
    user = store.add_user("operator", "operator@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(
        requested_by=user.id,
        source_connection_id=source.id,
        destination_connection_id=destination.id,
        source_paths=["/source"],
        destination_path="/destination",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    smtp_id = uuid.uuid4()
    await store.asave_smtp_settings(
        SMTPSettingsRecord(
            id=smtp_id,
            host="saved.smtp.test",
            port=2525,
            use_tls=True,
            from_address="saved@example.test",
            encrypted_credentials=CredentialCipher(b"m" * 32).encrypt(
                smtp_id,
                {"username": "saved-smtp-user", "password": password},
            ),
        )
    )

    async def close_store():
        return None

    store.aclose = close_store
    captured = {}

    class FailingSMTP:
        def __init__(self, host, port, timeout):
            captured["connection"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def starttls(self, **_kwargs):
            captured["starttls"] = True

        def login(self, username, supplied_password):
            captured["credentials"] = (username, supplied_password)
            raise smtplib.SMTPAuthenticationError(535, b"credentials rejected")

        def send_message(self, _message):
            raise AssertionError("message must not be sent after authentication fails")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(notifications.smtplib, "SMTP", FailingSMTP)

    with pytest.raises(NotificationDeliveryError) as raised:
        await worker._send_notification(transfer.id)

    assert captured["connection"] == ("saved.smtp.test", 2525, 20)
    assert captured["credentials"] == ("saved-smtp-user", password)
    assert str(raised.value) == "No fue posible entregar la notificación SMTP"
    assert password not in str(raised.value)
    assert transfer.status == "COMPLETED"
