import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


async def _client(app, username: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": username, "password": "A-Strong-Password123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    return client


def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ctm_csrf", "")}


async def _failed_transfer(store: MemoryStore, user_id: uuid.UUID, *, files: int = 1):
    source = store.create_connection("Mi MEGA", "MEGA", user_id, encrypted_credentials=b"source")
    destination = store.create_connection(
        "Mi Seafile", "SEAFILE", user_id, encrypted_credentials=b"destination"
    )
    transfer = await store.acreate_transfer(
        user_id, source.id, destination.id, ["/Cursos"], "/Backup"
    )
    transfer.status = "FAILED"
    transfer.attempt_count = 3
    transfer.started_at = datetime.now(UTC)
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    entries = [
        {
            "source_path": f"/Cursos/Unidad {index}/material-{index}.bin",
            "destination_path": f"/Backup/Unidad {index}/material-{index}.bin",
            "size_bytes": index + 1,
        }
        for index in range(files)
    ]
    if files:
        entries[-1] = {
            "source_path": "/Cursos/Idiomas/Curso Pimsleur.rar",
            "destination_path": "/Backup/Idiomas/Curso Pimsleur.rar",
            "size_bytes": 686_289_042,
        }
    await store.aadd_transfer_manifest_items(transfer.id, entries)
    manifest = await store.alist_transfer_manifest_items(transfer.id, limit=max(files, 1))
    failed = next(item for item in manifest if item.source_path.endswith("Curso Pimsleur.rar"))
    failed.status = "FAILED"
    failed.error_code = "MEGACMD_COMMAND_FAILED"
    failed.last_failure_stage = "SOURCE_DOWNLOAD"
    failed.last_failure_attempt = 3
    await store.aadd_transfer_event(
        transfer.id,
        level="ERROR",
        event_type="FAILURE_DIAGNOSTIC",
        message="No se pudo copiar un archivo del origen.",
        provider_error_code=failed.error_code,
        technical_details={
            "manifest_item_id": str(failed.id),
            "provider": "MEGA",
            "provider_stage": "SOURCE_DOWNLOAD",
            "attempt": 3,
            "destination_write_started": False,
            "retryable": False,
        },
    )
    return source, destination, transfer, failed


@pytest.mark.asyncio
async def test_failed_incident_is_first_page_with_exact_routes_and_skip_action():
    store = MemoryStore()
    user = store.add_user(
        "incident-owner",
        "incident-owner@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    _source, _destination, transfer, failed = await _failed_transfer(store, user.id, files=350)
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, user.username)

    response = await client.get("/api/v1/transfer-incidents?kind=failed&limit=1")
    await client.aclose()

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"] == {"failed": 1, "omitted": 0}
    assert payload["next_cursor"] is None
    row = payload["items"][0]
    assert row["transfer"] == {
        "id": str(transfer.id),
        "public_id": transfer.public_id,
        "status": "FAILED",
        "attempt_count": 3,
        "source_connection": {"name": "Mi MEGA", "provider_type": "MEGA"},
        "destination_connection": {"name": "Mi Seafile", "provider_type": "SEAFILE"},
    }
    assert {
        "id": str(failed.id),
        "source_path": "/Cursos/Idiomas/Curso Pimsleur.rar",
        "destination_path": "/Backup/Idiomas/Curso Pimsleur.rar",
        "requested_destination_path": "/Backup/Idiomas/Curso Pimsleur.rar",
        "error_code": "MEGACMD_COMMAND_FAILED",
        "omission_kind": None,
        "omitted_at": None,
        "size_bytes": 686_289_042,
    }.items() <= row["item"].items()
    assert row["can_skip"] is True
    assert row["skip_unavailable_reason"] is None
    assert row["can_retry_omission"] is False


@pytest.mark.asyncio
async def test_omitted_incidents_exclude_legacy_skipped_and_are_owner_scoped():
    store = MemoryStore()
    owner = store.add_user(
        "incident-owner-2",
        "incident-owner-2@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    outsider = store.add_user(
        "incident-outsider",
        "incident-outsider@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    _source, _destination, transfer, _failed = await _failed_transfer(store, owner.id)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/Cursos/explicit.rar",
                "destination_path": "/Backup/explicit.rar",
                "size_bytes": 9,
            },
            {
                "source_path": "/Cursos/legacy-skip.rar",
                "destination_path": "/Backup/legacy-skip.rar",
                "size_bytes": 10,
            },
        ],
    )
    rows = await store.alist_transfer_manifest_items(transfer.id, limit=10)
    explicit = next(item for item in rows if item.source_path.endswith("explicit.rar"))
    explicit.status = "SKIPPED"
    explicit.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    explicit.omitted_at = datetime.now(UTC)
    legacy = next(item for item in rows if item.source_path.endswith("legacy-skip.rar"))
    legacy.status = "SKIPPED"
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    owner_client = await _client(app, owner.username)
    outsider_client = await _client(app, outsider.username)

    owner_response = await owner_client.get("/api/v1/transfer-incidents?kind=omitted")
    outsider_response = await outsider_client.get("/api/v1/transfer-incidents?kind=omitted")
    await owner_client.aclose()
    await outsider_client.aclose()

    assert owner_response.status_code == 200
    assert owner_response.json()["counts"] == {"failed": 1, "omitted": 1}
    assert [row["item"]["source_path"] for row in owner_response.json()["items"]] == [
        "/Cursos/explicit.rar"
    ]
    assert outsider_response.status_code == 200
    assert outsider_response.json() == {
        "items": [],
        "counts": {"failed": 0, "omitted": 0},
        "next_cursor": None,
    }


@pytest.mark.asyncio
async def test_pause_requests_boundary_for_running_transfer_and_is_idempotent():
    store = MemoryStore()
    user = store.add_user(
        "pause-owner", "pause-owner@example.test", PasswordService().hash("A-Strong-Password123!")
    )
    _source, _destination, transfer, _failed = await _failed_transfer(store, user.id)
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, user.username)

    first = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    second = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    await client.aclose()

    assert first.status_code == 202
    assert first.json()["status"] == "PAUSING"
    assert first.json()["pause_requested_at"] is not None
    assert second.status_code == 202
    assert second.json()["state_version"] >= first.json()["state_version"]


@pytest.mark.asyncio
async def test_pause_hides_transfer_from_another_owner():
    store = MemoryStore()
    owner = store.add_user(
        "pause-private-owner",
        "pause-private-owner@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    outsider = store.add_user(
        "pause-private-outsider",
        "pause-private-outsider@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    source = store.create_connection("MOCK origen", "MOCK", owner.id)
    destination = store.create_connection("MOCK destino", "MOCK", owner.id)
    transfer = await store.acreate_transfer(
        owner.id, source.id, destination.id, ["/Documents"], "/Backup"
    )
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, outsider.username)

    response = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    await client.aclose()

    assert response.status_code == 404
    assert (await store.aget_transfer(transfer.id)).status == "QUEUED"


@pytest.mark.asyncio
async def test_pause_dispatch_race_returns_legacy_bulk_unavailable_reason():
    store = MemoryStore()
    user = store.add_user(
        "pause-race-owner",
        "pause-race-owner@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    source = store.create_connection("MEGA origen", "MEGA", user.id, encrypted_credentials=b"s")
    destination = store.create_connection(
        "Drive destino", "GOOGLE_DRIVE", user.id, encrypted_credentials=b"d"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/source.rar"], "/backup"
    )
    transfer.conflict_policy = "SKIP"
    await store.asave_transfer(transfer)
    original_pause = store.apause_transfer

    async def start_legacy_dispatch_before_pause(transfer_id, now):
        current = store.transfers[transfer_id]
        current.execution_started_at = now
        current.dispatch_token = uuid.uuid4()
        return await original_pause(transfer_id, now)

    store.apause_transfer = start_legacy_dispatch_before_pause
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, user.username)

    response = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    await client.aclose()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LEGACY_BULK_EXECUTION"
    assert (await store.aget_transfer(transfer.id)).status == "QUEUED"


@pytest.mark.asyncio
async def test_unstarted_schedule_pauses_immediately_and_resumes_without_new_attempt():
    store = MemoryStore()
    user = store.add_user(
        "pause-schedule",
        "pause-schedule@example.test",
        PasswordService().hash("A-Strong-Password123!"),
    )
    source = store.create_connection("MOCK origen", "MOCK", user.id)
    destination = store.create_connection("MOCK destino", "MOCK", user.id)
    scheduled_at = datetime.now(UTC) + timedelta(days=1)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup", scheduled_at=scheduled_at
    )
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, user.username)

    paused = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    resumed = await client.post(
        f"/api/v1/transfers/{transfer.id}/resume", headers=_csrf_headers(client)
    )
    await client.aclose()

    assert paused.status_code == 202
    assert paused.json()["status"] == "PAUSED"
    assert (await store.aget_transfer(transfer.id)).dispatch_token is None
    assert resumed.status_code == 202
    assert resumed.json()["status"] == "SCHEDULED"
    assert resumed.json()["attempt_count"] == transfer.attempt_count
    assert resumed.json()["scheduled_at"] == scheduled_at.isoformat()


@pytest.mark.asyncio
async def test_active_legacy_bulk_transfer_reports_pause_unavailable_reason():
    store = MemoryStore()
    user = store.add_user(
        "pause-legacy", "pause-legacy@example.test", PasswordService().hash("A-Strong-Password123!")
    )
    source = store.create_connection("Drive origen", "GOOGLE_DRIVE", user.id)
    destination = store.create_connection("Seafile destino", "SEAFILE", user.id)
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Documents"], "/Backup", conflict_policy="SKIP"
    )
    transfer.status = "RUNNING"
    transfer.dispatch_token = uuid.uuid4()
    transfer.execution_started_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    client = await _client(app, user.username)

    response = await client.post(
        f"/api/v1/transfers/{transfer.id}/pause", headers=_csrf_headers(client)
    )
    await client.aclose()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LEGACY_BULK_EXECUTION"
    assert (await store.aget_transfer(transfer.id)).status == "RUNNING"
