import uuid

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


async def logged_in_client(app, username: str):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username, "password": "A-Strong-Password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    return client


async def failed_smb_transfer(store: MemoryStore, user_id: uuid.UUID):
    source = store.create_connection(
        "NAS", "SMB", user_id, encrypted_credentials=b"source-encrypted"
    )
    destination = store.create_connection(
        "Drive", "GOOGLE_DRIVE", user_id, encrypted_credentials=b"destination-encrypted"
    )
    transfer = await store.acreate_transfer(
        user_id, source.id, destination.id, ["/Projects"], "/Archive"
    )
    transfer.status = "FAILED"
    transfer.error_count = 1
    transfer.completed_at = None
    await store.asave_transfer(transfer)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/Projects/report.pdf",
                "destination_path": "/Archive/Projects/report.pdf",
                "is_dir": False,
                "size_bytes": 123,
                "modified_at": None,
            }
        ],
    )
    return source, destination, transfer


@pytest.mark.asyncio
async def test_smb_transfer_report_is_paginated_and_resume_rejects_changed_credentials(
    monkeypatch,
):
    store = MemoryStore()
    user = store.add_user(
        "smb-admin",
        "smb-admin@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="ADMIN",
    )
    source, _destination, transfer = await failed_smb_transfer(store, user.id)
    report_file = (
        await store.alist_transfer_manifest_items(
            transfer.id, statuses={"PENDING"}, after_id=None, limit=10
        )
    )[0]
    await store.aupdate_transfer_manifest_item(
        transfer.id, report_file.id, status="FAILED", error_code="NETWORK"
    )
    changed_source = await store.aget_connection(source.id)
    assert changed_source is not None
    changed_source.encrypted_credentials = b"new-share-or-password"
    await store.asave_connection(changed_source)
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
    client = await logged_in_client(app, user.username)
    report = await client.get(f"/api/v1/transfers/{transfer.id}/items?limit=1")
    resume = await client.post(
        f"/api/v1/transfers/{transfer.id}/resume",
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    await client.aclose()

    assert report.status_code == 200
    assert len(report.json()["items"]) == 1
    assert report.json()["items"][0]["status"] == "FAILED"
    assert resume.status_code == 409
    assert "credenciales" in resume.json()["detail"].lower()


@pytest.mark.asyncio
async def test_smb_transfer_resume_requeues_failed_items_and_clears_cancel(monkeypatch):
    store = MemoryStore()
    user = store.add_user(
        "resume-admin",
        "resume-admin@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="ADMIN",
    )
    _source, _destination, transfer = await failed_smb_transfer(store, user.id)
    manifest_file = (
        await store.alist_transfer_manifest_items(
            transfer.id, statuses={"PENDING"}, after_id=None, limit=10
        )
    )[0]
    await store.aupdate_transfer_manifest_item(
        transfer.id, manifest_file.id, status="FAILED", error_code="NETWORK"
    )
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    dispatch_requests: list[bool] = []

    def enqueue_dispatch() -> None:
        dispatch_requests.append(True)

    monkeypatch.setattr("app.worker.dispatch_pending_transfers.delay", enqueue_dispatch)
    app.state.cancel_requests.add(transfer.id)
    client = await logged_in_client(app, user.username)
    response = await client.post(
        f"/api/v1/transfers/{transfer.id}/resume",
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    await client.aclose()
    resumed = await store.aget_transfer(transfer.id)

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert dispatch_requests == [True]
    assert transfer.id not in app.state.cancel_requests
    assert resumed is not None and resumed.attempt_count == 2
