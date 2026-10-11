import uuid

import pytest

from app.services.audit import AuditLogger


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
async def test_terminal_transfer_notification_is_enqueued_separately():
    from types import SimpleNamespace

    from app.services.notifications import enqueue_transfer_notification

    transfer_id = uuid.uuid4()
    transfer = SimpleNamespace(
        id=transfer_id,
        status="COMPLETED",
        send_email_notification=True,
    )

    class Store:
        async def aget_transfer(self, requested_id):
            return transfer if requested_id == transfer_id else None

    class Task:
        def __init__(self):
            self.ids = []

        def delay(self, requested_id):
            self.ids.append(requested_id)

    task = Task()
    assert await enqueue_transfer_notification(Store(), transfer_id, task)
    assert task.ids == [str(transfer_id)]
