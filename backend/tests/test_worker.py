import pytest

import app.worker as worker
from app.services.memory_store import MemoryStore
from app.services.smtp_config import SMTPServerConfig
from app.settings import Settings
from app.worker import celery_app


def test_transfer_and_notification_tasks_use_worker_queue():
    routes = celery_app.conf.task_routes

    assert routes["app.worker.run_transfer"]["queue"] == "transfers"
    assert routes["app.worker.send_transfer_notification"]["queue"] == "transfers"
    assert celery_app.conf.task_default_queue == "transfers"


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
