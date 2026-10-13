import asyncio
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import AuditEvent, StorageConnection, TransferCounter
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.database_store import DatabaseStore
from app.services.memory_store import (
    ConnectionInUseError,
    ConnectionUnavailableError,
    SMTPSettingsRecord,
)
from app.settings import Settings


@pytest.mark.asyncio
async def test_transfer_execution_lock_is_exclusive_and_released_after_use():
    class FakeEngine:
        def __init__(self):
            self.held_keys: set[int] = set()

        def connect(self):
            return FakeConnection(self)

    class FakeConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self, engine):
            self.engine = engine

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement, parameters):
            lock_key = parameters["lock_key"]
            if lock_key in self.engine.held_keys:
                return False
            self.engine.held_keys.add(lock_key)
            return True

        async def execute(self, _statement, parameters):
            self.engine.held_keys.discard(parameters["lock_key"])

        async def commit(self):
            return None

    store = object.__new__(DatabaseStore)
    store.engine = FakeEngine()
    transfer_id = uuid.uuid4()

    async with store.transfer_execution_lock(transfer_id) as acquired:
        assert acquired is True
        async with store.transfer_execution_lock(transfer_id) as duplicate_acquired:
            assert duplicate_acquired is False
        assert len(store.engine.held_keys) == 1

    assert store.engine.held_keys == set()
    async with store.transfer_execution_lock(transfer_id) as acquired_after_release:
        assert acquired_after_release is True


def test_transfer_counter_year_is_not_database_generated():
    assert TransferCounter.__table__.c.year.autoincrement is False


@pytest.mark.asyncio
async def test_database_store_atomically_creates_one_smtp_settings_identity(tmp_path):
    key_file = tmp_path / "master_key"
    key_file.write_bytes(b"k" * 32)
    cipher = CredentialCipher.from_secret_file(key_file)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'smtp-race.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    candidates = []
    for index in range(2):
        candidate_id = uuid.uuid4()
        candidates.append(
            SMTPSettingsRecord(
                id=candidate_id,
                host=f"smtp{index}.example.test",
                port=587,
                use_tls=True,
                from_address=f"sender{index}@example.test",
                encrypted_credentials=cipher.encrypt(
                    candidate_id,
                    {"username": f"user-{index}", "password": f"pass-{index}"},
                ),
            )
        )

    returned = await asyncio.gather(
        *(store.acreate_smtp_settings_if_absent(candidate) for candidate in candidates)
    )
    persisted = await store.aget_smtp_settings()
    await store.aclose()

    assert persisted is not None
    assert returned[0].id == returned[1].id == persisted.id
    decrypted = cipher.decrypt(persisted.id, persisted.encrypted_credentials)
    assert decrypted in (
        {"username": "user-0", "password": "pass-0"},
        {"username": "user-1", "password": "pass-1"},
    )


@pytest.mark.asyncio
async def test_database_store_persists_user_and_encrypted_connection(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ctm.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user(
        username="persistent",
        email="persistent@example.test",
        password_hash="$argon2id$test",
        role="ADMIN",
    )
    connection_id = uuid.uuid4()
    connection = await store.acreate_connection(
        "MEGA encrypted",
        "MEGA",
        user.id,
        connection_id,
        b"\x01\x02encrypted",
        adapter_type="RCLONE",
    )
    transfer = await store.acreate_transfer(
        user.id, connection.id, connection.id, ["/source"], "/destination"
    )
    transfer.current_speed_bps = 512
    await store.asave_transfer(transfer)
    await store.aadd_transfer_event(
        transfer.id,
        level="INFO",
        event_type="QUEUED",
        message="Transfer queued",
        technical_details={"bytes_transferred": 0, "refresh_token": "must-not-persist"},
    )
    await store.aclose()

    reopened = DatabaseStore(settings)
    loaded_user = await reopened.aget_user(user.id)
    loaded_connection = await reopened.aget_connection(connection_id)
    loaded_events = await reopened.alist_transfer_events(transfer.id)
    loaded_transfer = await reopened.aget_transfer(transfer.id)
    await reopened.aclose()

    assert loaded_user is not None
    assert loaded_user.username == "persistent"
    assert loaded_connection is not None
    assert loaded_connection.encrypted_credentials == b"\x01\x02encrypted"
    assert loaded_connection.adapter_type == "RCLONE"
    assert loaded_transfer is not None
    assert loaded_transfer.current_speed_bps == 512
    assert loaded_events[0]["technical_details"] == {
        "bytes_transferred": 0,
        "refresh_token": "[REDACTED]",
    }
    assert loaded_events[0]["correlation_id"] == str(transfer.id)


@pytest.mark.asyncio
async def test_database_store_transfer_counter_is_unique_and_api_uses_persistent_store(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.sqlite'}",
        app_secret_key="test-secret-key",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user(
        username="persistent-admin",
        email="persistent-admin@example.test",
        password_hash=PasswordService().hash("Strong-password123!"),
        role="ADMIN",
    )
    app = create_app(settings, store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        response = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "persistent-admin", "password": "Strong-password123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        csrf = client.cookies["ctm_csrf"]
        headers = {"X-CSRF-Token": csrf}
        source = await client.post(
            "/api/v1/connections",
            json={"name": "Source", "provider_type": "MOCK"},
            headers=headers,
        )
        destination = await client.post(
            "/api/v1/connections",
            json={"name": "Destination", "provider_type": "MOCK"},
            headers=headers,
        )
        payload = {
            "source_connection_id": source.json()["id"],
            "destination_connection_id": destination.json()["id"],
            "source_paths": ["/Proyectos/informe.pdf"],
            "destination_path": "/Backup",
        }
        first = await client.post("/api/v1/transfers", json=payload, headers=headers)
        second = await client.post("/api/v1/transfers", json=payload, headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json()["public_id"] != second.json()["public_id"]

        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            transfer = await store.aget_transfer(uuid.UUID(first.json()["id"]))
            if transfer and transfer.status == "COMPLETED":
                break
            await asyncio.sleep(0.02)

    await store.aclose()
    reopened = DatabaseStore(settings)
    persisted = await reopened.aget_transfer(uuid.UUID(first.json()["id"]))
    await reopened.aclose()
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert persisted.requested_by == user.id


@pytest.mark.asyncio
async def test_database_store_archives_connection_and_keeps_transfer_history(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'archive.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    admin = await store.acreate_user(
        "archive-admin", "archive-admin@example.test", "hash", "ADMIN"
    )
    member = await store.acreate_user("archive-member", "archive-member@example.test", "hash")
    source = await store.acreate_connection(
        "Private source", "GOOGLE_DRIVE", admin.id, uuid.uuid4(), b"encrypted-secret", False
    )
    destination = await store.acreate_connection(
        "Destination", "MOCK", admin.id, uuid.uuid4(), b""
    )
    await store.agrant_connection(member.id, source.id, admin.id)
    transfer = await store.acreate_transfer(
        admin.id, source.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = "COMPLETED"
    await store.asave_transfer(transfer)
    await store.aadd_transfer_event(
        transfer.id, level="INFO", event_type="COMPLETED", message="Copy completed"
    )

    stale_source = replace(source)
    archived = await store.aarchive_connection(source.id, actor_user_id=admin.id)
    with pytest.raises(ConnectionUnavailableError) as transfer_error:
        await store.acreate_transfer(
            admin.id, source.id, destination.id, ["/new.pdf"], "/archive"
        )
    assert transfer_error.value.reason == "not_found"
    with pytest.raises(ConnectionUnavailableError):
        await store.agrant_connection(member.id, source.id, admin.id)
    with pytest.raises(ConnectionUnavailableError):
        await store.asave_connection(stale_source)
    await store.aclose()

    reopened = DatabaseStore(settings)
    persisted = await reopened.aarchive_connection(source.id, actor_user_id=admin.id)
    historical_transfer = await reopened.aget_transfer(transfer.id)
    historical_events = await reopened.alist_transfer_events(transfer.id)
    visible_connections = await reopened.alist_connections()
    has_access = await reopened.ahas_connection_access(member.id, source.id)
    await reopened.aclose()

    assert archived is not None
    assert archived.deleted_at is not None
    assert archived.encrypted_credentials == b""
    assert persisted is None
    assert historical_transfer is not None
    assert historical_transfer.source_connection_id == source.id
    assert historical_transfer.status == "COMPLETED"
    assert historical_events[0]["message"] == "Copy completed"
    assert all(connection.id != source.id for connection in visible_connections)
    assert not has_access


@pytest.mark.asyncio
async def test_database_store_rolls_back_archive_when_audit_insert_fails(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'archive-audit-rollback.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    admin = await store.acreate_user("audit-admin", "audit-admin@example.test", "hash", "ADMIN")
    connection = await store.acreate_connection(
        "Source", "MOCK", admin.id, uuid.uuid4(), b"encrypted-secret"
    )

    def fail_audit_insert(session, _flush_context, _instances):
        if session.bind is store.engine.sync_engine and any(
            isinstance(row, AuditEvent) for row in session.new
        ):
            raise RuntimeError("audit persistence failed")

    event.listen(Session, "before_flush", fail_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="audit persistence failed"):
            await store.aarchive_connection(connection.id, actor_user_id=admin.id)
    finally:
        event.remove(Session, "before_flush", fail_audit_insert)

    async with store.sessions() as session:
        persisted = await session.get(StorageConnection, connection.id)
    assert persisted is not None
    assert persisted.deleted_at is None
    assert persisted.encrypted_credentials == b"encrypted-secret"
    await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["QUEUED", "VALIDATING", "RUNNING", "RETRYING", "CANCELLING"]
)
async def test_database_store_rejects_archiving_connection_with_active_transfer(
    tmp_path, status: str
):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'{status}.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    admin = await store.acreate_user(
        f"admin-{status}", f"{status}@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection(
        "Source", "MOCK", admin.id, uuid.uuid4(), b"encrypted-secret"
    )
    destination = await store.acreate_connection(
        "Destination", "MOCK", admin.id, uuid.uuid4(), b""
    )
    transfer = await store.acreate_transfer(
        admin.id, source.id, destination.id, ["/report.pdf"], "/archive"
    )
    transfer.status = status
    await store.asave_transfer(transfer)

    with pytest.raises(ConnectionInUseError):
        await store.aarchive_connection(source.id, actor_user_id=admin.id)

    connection = await store.aget_connection(source.id)
    assert connection is not None
    assert connection.encrypted_credentials == b"encrypted-secret"
    assert (await store.aget_transfer(transfer.id)).status == status
    await store.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
async def test_postgres_serializes_connection_archive_with_transfer_creation():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"], session_cookie_secure=False)
    store = DatabaseStore(settings)
    assert store.engine.dialect.name == "postgresql"
    await store.create_schema()
    user_suffix = uuid.uuid4()
    admin = await store.acreate_user(
        f"race-{user_suffix}", f"race-{user_suffix}@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("Source", "MOCK", admin.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection(
        "Destination", "MOCK", admin.id, uuid.uuid4(), b""
    )
    start = asyncio.Event()

    async def archive():
        await start.wait()
        try:
            return await store.aarchive_connection(source.id, actor_user_id=admin.id)
        except ConnectionInUseError:
            return "blocked"

    async def create_transfer():
        await start.wait()
        try:
            return await store.acreate_transfer(
                admin.id, source.id, destination.id, ["/report.pdf"], "/archive"
            )
        except ConnectionUnavailableError as exc:
            return exc

    archive_task = asyncio.create_task(archive())
    transfer_task = asyncio.create_task(create_transfer())
    start.set()
    archive_result, transfer_result = await asyncio.gather(archive_task, transfer_task)

    if archive_result == "blocked":
        assert not isinstance(transfer_result, ConnectionUnavailableError)
        assert transfer_result is not None
        assert await store.aget_transfer(transfer_result.id) is not None
    else:
        assert archive_result is not None
        assert isinstance(transfer_result, ConnectionUnavailableError)
        assert transfer_result.reason == "not_found"
        transfers = await store.alist_transfers()
        assert all(
            transfer.source_connection_id != source.id
            and transfer.destination_connection_id != source.id
            for transfer in transfers
        )
    await store.aclose()
