import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


async def logged_in_client(app, username: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username, "password": "A-Strong-Password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    return client


async def failed_mega_transfer(store: MemoryStore, user_id: uuid.UUID):
    source = store.create_connection("MEGA", "MEGA", user_id, encrypted_credentials=b"source")
    destination = store.create_connection(
        "Seafile", "SEAFILE", user_id, encrypted_credentials=b"destination"
    )
    transfer = await store.acreate_transfer(
        user_id, source.id, destination.id, ["/Curso"], "/backup"
    )
    transfer.status = "FAILED"
    transfer.attempt_count = 2
    transfer.started_at = datetime.now(UTC) - timedelta(minutes=1)
    transfer.files_total = 4317
    transfer.files_completed = 1978
    transfer.bytes_total = 265_726_536_099
    transfer.bytes_transferred = 265_040_247_057
    transfer.error_count = 1
    transfer.warning_count = 1
    transfer.completed_at = datetime.now(UTC)
    transfer.source_provider_type = "MEGA"
    await store.asave_transfer(transfer)

    entries = [
        {
            "source_path": f"/Curso/file-{index}.rar",
            "destination_path": f"/backup/Curso/file-{index}.rar",
            "size_bytes": 1,
        }
        for index in range(4317)
    ]
    entries[1977]["source_path"] = "/Curso/Curso Pimsleur.rar"
    entries[1977]["destination_path"] = "/backup/Curso Pimsleur.rar"
    entries[1977]["size_bytes"] = 686_289_042
    await store.aadd_transfer_manifest_items(transfer.id, entries)
    manifest = await store.alist_transfer_manifest_items(
        transfer.id,
        statuses={"PENDING"},
        limit=5000,
    )
    failed_item = next(item for item in manifest if item.source_path.endswith("Curso Pimsleur.rar"))
    copied_items = [item for item in manifest if item.id != failed_item.id][:1977]
    for item in copied_items:
        item.status = "COPIED"
        item.verified_by = "SIZE"
    failed_item.status = "FAILED"
    failed_item.error_code = "MEGACMD_COMMAND_FAILED"
    await store.aadd_transfer_event(
        transfer.id,
        level="ERROR",
        event_type="FAILURE_DIAGNOSTIC",
        message="No se pudo copiar este archivo; se continúa con los restantes.",
        provider_error_code="MEGACMD_COMMAND_FAILED",
        technical_details={
            "file_name": "Curso Pimsleur.rar",
            "provider": "MEGA",
            "provider_stage": "SOURCE_DOWNLOAD",
            "operation": "SOURCE_DOWNLOAD",
            "attempt": 1,
            "destination_write_started": False,
        },
    )
    return source, destination, transfer, failed_item


async def parent_with_omissions_and_child(
    store: MemoryStore,
    user_id: uuid.UUID,
    *,
    parent_status: str = "COMPLETED_WITH_WARNINGS",
):
    source = store.create_connection("Parent source", "GOOGLE_DRIVE", user_id)
    destination = store.create_connection("Parent destination", "SEAFILE", user_id)
    parent = await store.acreate_transfer(
        user_id, source.id, destination.id, ["/parent"], "/destination"
    )
    parent.status = parent_status
    parent.attempt_count = 2
    parent.completed_at = datetime.now(UTC)
    await store.asave_transfer(parent)
    await store.aadd_transfer_manifest_items(
        parent.id,
        [
            {
                "source_path": "/parent/restricted.rar",
                "destination_path": "/destination/restricted.rar",
                "size_bytes": 8,
            }
        ],
    )
    omitted = (await store.alist_transfer_manifest_items(parent.id, statuses={"PENDING"}))[0]
    omitted.status = "SKIPPED"
    omitted.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    omitted.omitted_at = datetime.now(UTC)
    child = await store.acreate_transfer(
        user_id,
        source.id,
        destination.id,
        ["/child"],
        "/destination",
        depends_on_transfer_id=parent.id,
    )
    return parent, child


@pytest.mark.asyncio
async def test_skip_and_resume_is_atomic_audited_and_idempotent(monkeypatch):
    store = MemoryStore()
    user = store.add_user(
        "omission-admin",
        "omission-admin@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="ADMIN",
    )
    _source, _destination, transfer, failed_item = await failed_mega_transfer(store, user.id)
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    dispatches: list[bool] = []
    monkeypatch.setattr(
        "app.worker.dispatch_pending_transfers.delay", lambda: dispatches.append(True)
    )
    client = await logged_in_client(app, user.username)
    request_id = uuid.uuid4()
    body = {
        "expected_attempt": 2,
        "request_id": str(request_id),
        "reason": "USER_ACCEPTED_SOURCE_FAILURE",
    }
    url = f"/api/v1/transfers/{transfer.id}/items/{failed_item.id}/skip-and-resume"
    response = await client.post(
        url,
        json=body,
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    replay = await client.post(
        url,
        json=body,
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    reused_for_another_item = await client.post(
        f"/api/v1/transfers/{transfer.id}/items/{uuid.uuid4()}/skip-and-resume",
        json=body,
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    await client.aclose()

    skipped = await store.alist_transfer_manifest_items(transfer.id, statuses={"SKIPPED"})
    summary = (await store.alist_transfer_manifest_summaries([transfer.id]))[transfer.id]
    audits = await store.alist_audit_events(limit=100)
    assert response.status_code == 202
    assert response.json()["attempt_count"] == 3
    assert replay.status_code == 202
    assert replay.json()["attempt_count"] == 3
    assert reused_for_another_item.status_code == 409
    assert dispatches == [True]
    assert len(skipped) == 1
    assert skipped[0].omission_kind == "USER_ACCEPTED_SOURCE_FAILURE"
    assert skipped[0].omitted_by == user.id
    assert summary["copied"] == 1977
    assert summary["skipped"] == 1
    assert summary["omitted"] == 1
    assert summary["omitted_bytes"] == 686_289_042
    assert summary["pending"] == 2339
    assert summary["failed"] == 0
    assert transfer.files_completed == 1978
    assert transfer.bytes_transferred == 265_040_247_057
    assert transfer.error_count == 0
    assert transfer.status == "QUEUED"
    assert transfer.attempt_count == 3
    assert [event["action"] for event in audits[:2]] == [
        "TRANSFER_RESUMED",
        "TRANSFER_FILE_OMITTED",
    ]


@pytest.mark.asyncio
async def test_accept_parent_omissions_is_attempt_scoped_audited_and_idempotent(monkeypatch):
    store = MemoryStore()
    user = store.add_user(
        "parent-accept-owner",
        "parent-accept@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    parent, child = await parent_with_omissions_and_child(store, user.id)
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    dispatches = []
    monkeypatch.setattr(
        "app.worker.dispatch_pending_transfers.delay", lambda: dispatches.append("transfer")
    )
    monkeypatch.setattr(
        "app.worker.dispatch_pending_evaluations.delay", lambda: dispatches.append("evaluation")
    )
    client = await logged_in_client(app, user.username)
    request_id = uuid.uuid4()
    body = {"expected_parent_attempt": 2, "request_id": str(request_id)}
    url = f"/api/v1/transfers/{child.id}/accept-parent-omissions"
    headers = {"X-CSRF-Token": client.cookies["ctm_csrf"]}
    response = await client.post(url, json=body, headers=headers)
    replay = await client.post(url, json=body, headers=headers)
    reused = await client.post(
        url,
        json={"expected_parent_attempt": 3, "request_id": str(request_id)},
        headers=headers,
    )
    await client.aclose()

    audits = await store.alist_audit_events(limit=100)
    updated_child = await store.aget_transfer(child.id)
    assert response.status_code == 202
    assert response.json()["accepted_parent_omissions_attempt"] == 2
    assert replay.status_code == 202
    assert reused.status_code == 409
    assert updated_child is not None
    assert updated_child.accepted_parent_omissions_attempt == 2
    assert dispatches == ["transfer"]
    assert any(event["action"] == "TRANSFER_PARENT_OMISSIONS_ACCEPTED" for event in audits)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_status", ["FAILED", "CANCELLED"])
async def test_failed_or_cancelled_parent_omissions_cannot_be_accepted(
    monkeypatch, parent_status
):
    store = MemoryStore()
    user = store.add_user(
        f"parent-failure-{parent_status.lower()}",
        f"parent-{parent_status.lower()}@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    parent, child = await parent_with_omissions_and_child(
        store, user.id, parent_status=parent_status
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
    monkeypatch.setattr("app.worker.dispatch_pending_transfers.delay", lambda: None)
    client = await logged_in_client(app, user.username)
    response = await client.post(
        f"/api/v1/transfers/{child.id}/accept-parent-omissions",
        json={"expected_parent_attempt": parent.attempt_count, "request_id": str(uuid.uuid4())},
        headers={"X-CSRF-Token": client.cookies["ctm_csrf"]},
    )
    await client.aclose()

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_skip_and_resume_requires_csrf(monkeypatch):
    store = MemoryStore()
    user = store.add_user(
        "csrf-owner",
        "csrf-owner@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="ADMIN",
    )
    _source, _destination, transfer, item = await failed_mega_transfer(store, user.id)
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    monkeypatch.setattr("app.worker.dispatch_pending_transfers.delay", lambda: None)
    client = await logged_in_client(app, user.username)
    response = await client.post(
        f"/api/v1/transfers/{transfer.id}/items/{item.id}/skip-and-resume",
        json={
            "expected_attempt": 2,
            "request_id": str(uuid.uuid4()),
            "reason": "USER_ACCEPTED_SOURCE_FAILURE",
        },
    )
    await client.aclose()

    assert response.status_code == 403
    assert transfer.status == "FAILED"
    assert transfer.attempt_count == 2


@pytest.mark.asyncio
async def test_skip_and_resume_hides_transfers_and_manifest_items_outside_actor_scope(
    monkeypatch,
):
    store = MemoryStore()
    owner = store.add_user(
        "scope-owner",
        "scope-owner@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="USER",
    )
    outsider = store.add_user(
        "scope-outsider",
        "scope-outsider@example.test",
        PasswordService().hash("A-Strong-Password123!"),
        role="USER",
    )
    _source, _destination, transfer, item = await failed_mega_transfer(store, owner.id)
    app = create_app(
        Settings(
            app_env="production",
            app_secret_key="test-secret-key",
            session_cookie_secure=False,
        ),
        store=store,
    )
    app.state.event_bus = None
    monkeypatch.setattr("app.worker.dispatch_pending_transfers.delay", lambda: None)
    owner_client = await logged_in_client(app, owner.username)
    outsider_client = await logged_in_client(app, outsider.username)
    owner_headers = {"X-CSRF-Token": owner_client.cookies["ctm_csrf"]}
    outsider_headers = {"X-CSRF-Token": outsider_client.cookies["ctm_csrf"]}
    wrong_item = await owner_client.post(
        f"/api/v1/transfers/{transfer.id}/items/{uuid.uuid4()}/skip-and-resume",
        json={
            "expected_attempt": 2,
            "request_id": str(uuid.uuid4()),
            "reason": "USER_ACCEPTED_SOURCE_FAILURE",
        },
        headers=owner_headers,
    )
    invisible_transfer = await outsider_client.post(
        f"/api/v1/transfers/{transfer.id}/items/{item.id}/skip-and-resume",
        json={
            "expected_attempt": 2,
            "request_id": str(uuid.uuid4()),
            "reason": "USER_ACCEPTED_SOURCE_FAILURE",
        },
        headers=outsider_headers,
    )
    await owner_client.aclose()
    await outsider_client.aclose()

    assert wrong_item.status_code == 404
    assert invisible_transfer.status_code == 404
    assert transfer.status == "FAILED"
    assert transfer.attempt_count == 2
