import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.memory_store import MemoryStore
from app.settings import Settings


async def _client_for_transfer(*, role: str = "USER"):
    settings = Settings(
        app_secret_key="test-secret",
        session_cookie_secure=False,
        transfer_max_files=37,
        smb_transfer_max_files=73,
    )
    store = MemoryStore(settings)
    owner = store.add_user(
        "evaluation-owner",
        "evaluation-owner@example.test",
        PasswordService().hash("SafePassword123!"),
        role=role,
    )
    source = store.create_connection("Source", "MOCK", owner.id)
    destination = store.create_connection("Destination", "MOCK", owner.id)
    transfer = store.create_transfer(
        owner.id,
        source.id,
        destination.id,
        ["/source"],
        "/destination",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    app = create_app(settings, store=store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    login = await client.post(
        "/api/v1/auth/login",
        json={"username_or_email": owner.username, "password": "SafePassword123!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert login.status_code == 200
    return (
        client,
        store,
        owner,
        transfer,
        {"X-CSRF-Token": client.cookies["ctm_csrf"]},
        app,
    )


@pytest.mark.asyncio
async def test_evaluation_retry_is_authorized_idempotent_and_returns_public_state():
    client, store, _owner, transfer, headers, _app = await _client_for_transfer()
    evaluation = transfer.evaluation
    assert evaluation is not None
    evaluation.status = "BLOCKED"
    evaluation.files_found = 5001
    evaluation.count_complete = False
    evaluation.safe_error_code = "TRANSFER_FILE_LIMIT_EXCEEDED"
    evaluation.safe_message = "La selección supera el límite."
    evaluation.generation = 4
    evaluation.sequence = 9

    first = await client.post(f"/api/v1/transfers/{transfer.id}/evaluation", headers=headers)
    second = await client.post(f"/api/v1/transfers/{transfer.id}/evaluation", headers=headers)
    current = await store.aget_transfer(transfer.id)
    await client.aclose()

    assert first.status_code == 202, first.text
    assert first.json()["status"] == "PENDING"
    assert first.json()["generation"] == 5
    assert first.json()["files_found"] is None
    assert second.status_code == 202
    assert second.json()["generation"] == 5
    assert current is not None and current.evaluation is not None
    assert current.evaluation.attempt_count == 0
    assert "claim_token" not in first.json()
    assert "source_credential_version" not in first.json()


@pytest.mark.asyncio
async def test_evaluation_retry_requires_csrf_and_rejects_admitted_or_terminal_jobs():
    client, _store, _owner, transfer, headers, _app = await _client_for_transfer()
    missing_csrf = await client.post(f"/api/v1/transfers/{transfer.id}/evaluation")
    transfer.dispatch_token = uuid.uuid4()
    admitted = await client.post(f"/api/v1/transfers/{transfer.id}/evaluation", headers=headers)
    transfer.dispatch_token = None
    transfer.status = "COMPLETED"
    terminal = await client.post(f"/api/v1/transfers/{transfer.id}/evaluation", headers=headers)
    await client.aclose()

    assert missing_csrf.status_code == 403
    assert admitted.status_code == 409
    assert terminal.status_code == 409


@pytest.mark.asyncio
async def test_evaluation_payload_is_consistent_in_get_list_and_sse():
    client, _store, _owner, transfer, _headers, _app = await _client_for_transfer(role="ADMIN")
    assert transfer.evaluation is not None
    transfer.evaluation.status = "EVALUATING"
    transfer.evaluation.files_found = 3200
    transfer.evaluation.count_complete = False
    transfer.evaluation.sequence = 3
    transfer.status = "COMPLETED"

    single = await client.get(f"/api/v1/transfers/{transfer.id}")
    collection = await client.get("/api/v1/transfers")
    stream = await client.get(f"/api/v1/transfers/{transfer.id}/stream")
    await client.aclose()

    assert single.status_code == collection.status_code == stream.status_code == 200
    single_evaluation = single.json()["evaluation"]
    list_evaluation = next(
        item["evaluation"] for item in collection.json()["items"] if item["id"] == str(transfer.id)
    )
    frame = next(line[6:] for line in stream.text.splitlines() if line.startswith("data: "))
    stream_evaluation = json.loads(frame)["evaluation"]
    assert single_evaluation == list_evaluation == stream_evaluation
    assert single_evaluation["files_found"] == 3200
    assert single_evaluation["count_complete"] is False
    assert "claim_token" not in single_evaluation


@pytest.mark.asyncio
async def test_transfer_capabilities_expose_cloud_and_smb_file_limits():
    client, _store, _owner, _transfer, _headers, _app = await _client_for_transfer(role="ADMIN")
    response = await client.get("/api/v1/transfers/capabilities")
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["max_files_by_source_provider"] == {"default": 37, "SMB": 73}


@pytest.mark.asyncio
async def test_admin_can_retry_another_users_evaluation_but_other_user_gets_not_found():
    owner_client, store, _owner, transfer, _headers, app = await _client_for_transfer()
    admin = store.add_user(
        "evaluation-admin",
        "evaluation-admin@example.test",
        PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    other = store.add_user(
        "evaluation-other",
        "evaluation-other@example.test",
        PasswordService().hash("SafePassword123!"),
    )

    async def authenticated_as(username: str):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        response = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": username, "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        return client, {"X-CSRF-Token": client.cookies["ctm_csrf"]}

    admin_client, admin_headers = await authenticated_as(admin.username)
    other_client, other_headers = await authenticated_as(other.username)
    admin_response = await admin_client.post(
        f"/api/v1/transfers/{transfer.id}/evaluation", headers=admin_headers
    )
    other_response = await other_client.post(
        f"/api/v1/transfers/{transfer.id}/evaluation", headers=other_headers
    )
    await owner_client.aclose()
    await admin_client.aclose()
    await other_client.aclose()

    assert admin_response.status_code == 202
    assert other_response.status_code == 404
