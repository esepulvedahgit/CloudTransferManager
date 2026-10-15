import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.audit import AuditLogger
from app.services.memory_store import MemoryStore
from app.settings import Settings


class CapturingStore:
    def __init__(self):
        self.events = []

    async def aadd_audit_event(self, **event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_audit_logger_redacts_passwords_and_tokens_before_persisting():
    store = CapturingStore()
    logger = AuditLogger(store)
    await logger.record(
        actor_user_id=uuid.uuid4(),
        action="CONNECTION_CREATED",
        resource_type="connection",
        result="SUCCESS",
        metadata={
            "provider_type": "MEGA",
            "password": "secret",
            "oauth": {"refresh_token": "token"},
        },
    )
    assert store.events[0]["metadata"] == {
        "provider_type": "MEGA",
        "password": "[REDACTED]",
        "oauth": {"refresh_token": "[REDACTED]"},
    }


def test_transfer_email_template_contains_safe_summary_without_source_paths():
    from types import SimpleNamespace

    from app.services.notifications import render_transfer_email

    transfer = SimpleNamespace(
        public_id="TRF-2026-000001",
        status="COMPLETED",
        bytes_transferred=1024,
        created_at=None,
        completed_at=None,
        source_paths=["/private-folder/secret-file.txt"],
        destination_path="/private-backup",
    )
    subject, body = render_transfer_email(transfer, "MEGA", "Drive")
    assert "TRF-2026-000001" in subject
    assert "MEGA" in body and "Drive" in body and "1.0 KB" in body
    assert "/private-folder/secret-file.txt" not in body
    assert "/private-backup" not in body


@pytest.mark.asyncio
async def test_admin_can_inspect_notification_outbox_without_recipient_data():
    store = MemoryStore()
    admin = store.add_user(
        "mail-admin",
        "mail-admin@example.test",
        PasswordService().hash("Admin-password123!"),
        role="ADMIN",
    )
    source = store.create_connection("Source", "MOCK", admin.id)
    destination = store.create_connection("Destination", "MOCK", admin.id)
    transfer = store.create_transfer(
        admin.id,
        source.id,
        destination.id,
        ["/private/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)

    app = create_app(Settings(app_secret_key="test-secret", session_cookie_secure=False), store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/notifications/outbox")).status_code == 401
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "mail-admin", "password": "Admin-password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        response = await client.get("/api/v1/notifications/outbox")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["transfer_public_id"] == transfer.public_id
    assert items[0]["status"] == "PENDING"
    assert "recipient" not in response.text
    assert "private/report" not in response.text
