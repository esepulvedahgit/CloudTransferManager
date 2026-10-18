import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import (
    AuditEvent,
    StorageConnection,
    TransferCounter,
    TransferJob,
    TransferNotificationOutbox,
    User,
)
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
async def test_database_store_aggregates_manifest_summaries_for_transfer_page(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'manifest-summary.sqlite'}"
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    user = await store.acreate_user(
        "manifest-owner", "manifest-owner@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("NAS", "SMB", user.id, uuid.uuid4(), b"source")
    destination = await store.acreate_connection(
        "Drive", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/NAS"], "/Archive"
    )
    entries = [
        {"source_path": "/folder", "destination_path": "/Archive/folder", "is_dir": True},
        {
            "source_path": "/copied.bin",
            "destination_path": "/Archive/copied.bin",
            "is_dir": False,
            "size_bytes": 4,
        },
        {
            "source_path": "/failed.bin",
            "destination_path": "/Archive/failed.bin",
            "is_dir": False,
            "size_bytes": 3,
        },
        {
            "source_path": "/skipped.bin",
            "destination_path": "/Archive/skipped.bin",
            "is_dir": False,
            "size_bytes": 2,
        },
        {
            "source_path": "/pending.bin",
            "destination_path": "/Archive/pending.bin",
            "is_dir": False,
            "size_bytes": 1,
        },
    ]

    try:
        await store.aadd_transfer_manifest_items(transfer.id, entries)
        items = await store.alist_transfer_manifest_items(
            transfer.id,
            statuses={"PENDING"},
            after_id=None,
            limit=10,
            include_directories=True,
        )
        statuses = {
            "/copied.bin": "COPIED",
            "/failed.bin": "FAILED",
            "/skipped.bin": "SKIPPED",
        }
        for item in items:
            if item.source_path in statuses:
                await store.aupdate_transfer_manifest_item(
                    transfer.id, item.id, status=statuses[item.source_path]
                )

        missing_id = uuid.uuid4()
        summaries = await store.alist_transfer_manifest_summaries([transfer.id, missing_id])
    finally:
        await store.aclose()

    assert summaries[transfer.id] == {
        "copied": 1,
        "failed": 1,
        "skipped": 1,
        "pending": 1,
        "renamed": 0,
        "omitted": 0,
        "omitted_bytes": 0,
    }
    assert summaries[missing_id] == {
        "copied": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
        "renamed": 0,
        "omitted": 0,
        "omitted_bytes": 0,
    }


@pytest.mark.asyncio
async def test_database_transfer_page_queries_only_requested_rows_and_returns_total(tmp_path):
    store = DatabaseStore(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'transfer-page.sqlite'}",
            session_cookie_secure=False,
        )
    )
    await store.create_schema()
    user = await store.acreate_user("page-user", "page-user@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfers = [
        await store.acreate_transfer(
            user.id, source.id, destination.id, [f"/file-{index}"], "/archive"
        )
        for index in range(5)
    ]

    first_page, total = await store.alist_transfer_page(user.id, offset=0, limit=2)
    second_page, second_total = await store.alist_transfer_page(user.id, offset=2, limit=2)
    await store.aclose()

    assert total == second_total == 5
    assert len(first_page) == len(second_page) == 2
    assert {record.id for record in first_page}.isdisjoint({record.id for record in second_page})
    assert {record.id for record in first_page + second_page}.issubset(
        {record.id for record in transfers}
    )


@pytest.mark.asyncio
async def test_database_store_lists_only_active_transfer_ids_for_staging_sweep(tmp_path):
    store = DatabaseStore(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'active-transfer-ids.sqlite'}",
            session_cookie_secure=False,
        )
    )
    await store.create_schema()
    user = await store.acreate_user("staging-owner", "staging-owner@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    active = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/active"], "/archive"
    )
    terminal = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/terminal"], "/archive"
    )
    active.status = "RUNNING"
    terminal.status = "COMPLETED"
    await store.asave_transfer(active)
    await store.asave_transfer(terminal)

    active_ids = await store.alist_active_transfer_ids()
    await store.aclose()

    assert active_ids == {active.id}


@pytest.mark.asyncio
async def test_transfer_creation_and_audit_event_share_one_database_transaction(tmp_path):
    store = DatabaseStore(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'transfer-audit.sqlite'}",
            session_cookie_secure=False,
        )
    )
    await store.create_schema()
    user = await store.acreate_user("atomic-audit", "atomic-audit@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    idempotency_key = uuid.uuid4()
    audit_event = {
        "ip_address": "192.0.2.20",
        "user_agent": "test-agent",
        "metadata": {"operation": "COPY", "password": "must-not-be-stored"},
    }

    def reject_audit_insert(_mapper, _connection, _target):
        raise RuntimeError("simulated audit insert failure")

    event.listen(AuditEvent, "before_insert", reject_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated audit insert failure"):
            await store.acreate_transfer(
                user.id,
                source.id,
                destination.id,
                ["/report.pdf"],
                "/archive",
                idempotency_key=idempotency_key,
                idempotency_request_hash="b" * 64,
                audit_event=audit_event,
            )
    finally:
        event.remove(AuditEvent, "before_insert", reject_audit_insert)
    assert await store.alist_transfers(requested_by=user.id) == []

    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        idempotency_key=idempotency_key,
        idempotency_request_hash="b" * 64,
        audit_event=audit_event,
    )
    replay = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        idempotency_key=idempotency_key,
        idempotency_request_hash="b" * 64,
        audit_event=audit_event,
    )
    audit_events = await store.alist_audit_events()
    await store.aclose()

    assert replay.id == transfer.id
    assert replay.idempotent_replay
    assert len(audit_events) == 1
    assert audit_events[0]["action"] == "TRANSFER_CREATED"
    assert audit_events[0]["resource_id"] == str(transfer.id)
    assert audit_events[0]["metadata"] == {
        "operation": "COPY",
        "password": "[REDACTED]",
        "public_id": transfer.public_id,
    }


@pytest.mark.asyncio
async def test_database_store_live_manifest_progress_does_not_count_failed_empty_files_as_copied(
    tmp_path,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'manifest-progress.sqlite'}"
    store = DatabaseStore(Settings(database_url=database_url))
    await store.create_schema()
    user = await store.acreate_user(
        "progress-owner", "progress-owner@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("NAS", "SMB", user.id, uuid.uuid4(), b"source")
    destination = await store.acreate_connection(
        "Drive", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/NAS"], "/Archive"
    )
    transfer.files_total = 1
    await store.asave_transfer(transfer)
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/empty.bin",
                "destination_path": "/Archive/empty.bin",
                "is_dir": False,
                "size_bytes": 0,
            }
        ],
    )
    item = (await store.alist_transfer_manifest_items(transfer.id))[0]

    await store.aupdate_transfer_manifest_item(transfer.id, item.id, status="FAILED")

    refreshed = await store.aget_transfer(transfer.id)
    await store.aclose()
    assert refreshed is not None
    assert refreshed.progress_percent < 100


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
async def test_stale_transfer_save_cannot_undo_a_cancel_request(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel-race.sqlite'}")
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user("cancel-owner", "cancel-owner@example.test", "hash", "ADMIN")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    created = await store.acreate_transfer(user.id, source.id, destination.id, ["/file"], "/copy")
    active = await store.aget_transfer(created.id)
    assert active is not None
    active.status = "RUNNING"
    await store.asave_transfer(active)
    stale = await store.aget_transfer(created.id)
    assert stale is not None
    cancel_time = stale.created_at

    cancelled, should_signal_worker = await store.acancel_transfer(created.id, cancel_time)
    assert cancelled is not None
    assert should_signal_worker is True
    stale.status = "RUNNING"
    stale.completed_at = None
    await store.asave_transfer(stale)

    persisted = await store.aget_transfer(created.id)
    assert persisted is not None
    assert persisted.status == "CANCELLING"
    assert persisted.cancel_requested_at == cancel_time
    assert await store.ais_transfer_cancel_requested(created.id) is True
    await store.aclose()


@pytest.mark.asyncio
async def test_stale_transfer_save_cannot_replace_a_terminal_state(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'terminal-race.sqlite'}")
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user(
        "terminal-owner", "terminal-owner@example.test", "hash", "ADMIN"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    created = await store.acreate_transfer(user.id, source.id, destination.id, ["/file"], "/copy")
    stale = await store.aget_transfer(created.id)
    completed = await store.aget_transfer(created.id)
    assert stale is not None
    assert completed is not None
    completed.status = "COMPLETED"
    completed.progress_percent = 100
    completed.completed_at = completed.created_at
    await store.asave_transfer(completed)

    stale.status = "CANCELLING"
    stale.progress_percent = 0
    stale.completed_at = None
    await store.asave_transfer(stale)

    persisted = await store.aget_transfer(created.id)
    await store.aclose()
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert persisted.progress_percent == 100
    assert persisted.completed_at == completed.created_at


@pytest.mark.asyncio
async def test_login_race_does_not_reactivate_user_or_restore_old_password(tmp_path):
    replacement_hash = PasswordService().hash("Replacement-password123!")

    class UserChangedAfterLookupStore(DatabaseStore):
        async def afind_user(self, username_or_email):
            user = await super().afind_user(username_or_email)
            if user is not None and not self.changed_after_lookup:
                self.changed_after_lookup = True
                async with self.sessions() as session:
                    row = await session.get(User, user.id)
                    assert row is not None
                    row.is_active = False
                    row.password_hash = replacement_hash
                    await session.commit()
            return user

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'login-race.sqlite'}",
        app_secret_key="test-session-secret",
        session_cookie_secure=False,
    )
    store = UserChangedAfterLookupStore(settings)
    store.changed_after_lookup = False
    await store.create_schema()
    await store.acreate_user(
        "login-race",
        "login-race@example.test",
        PasswordService().hash("Original-password123!"),
        "ADMIN",
    )
    app = create_app(settings, store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "username_or_email": "login-race",
                "password": "Original-password123!",
            },
            headers={"X-CSRF-Token": csrf},
        )

    persisted = await store.afind_user("login-race")
    await store.aclose()
    assert response.status_code == 401
    assert persisted is not None
    assert persisted.is_active is False
    assert persisted.password_hash == replacement_hash


@pytest.mark.asyncio
async def test_database_store_persists_idempotent_manifest_batches_and_credential_versions(
    tmp_path,
):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'manifest.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user(
        username="manifest-user",
        email="manifest-user@example.test",
        password_hash="$argon2id$test",
        role="ADMIN",
    )
    source = await store.acreate_connection(
        "NAS", "SMB", user.id, uuid.uuid4(), b"encrypted-source"
    )
    destination = await store.acreate_connection(
        "Drive", "GOOGLE_DRIVE", user.id, uuid.uuid4(), b"encrypted-destination"
    )
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/Projects"], "/Archive"
    )
    entries = [
        {
            "source_path": f"/Projects/{index:05d}.dat",
            "destination_path": f"/Archive/Projects/{index:05d}.dat",
            "is_dir": False,
            "size_bytes": index,
            "modified_at": None,
        }
        for index in range(10_001)
    ]
    inserted = await store.aadd_transfer_manifest_items(transfer.id, entries)
    repeated = await store.aadd_transfer_manifest_items(transfer.id, entries[:250])
    first_page = await store.alist_transfer_manifest_items(
        transfer.id, statuses={"PENDING"}, after_id=None, limit=200
    )
    await store.aupdate_transfer_manifest_item(
        transfer.id, first_page[0].id, status="COPIED", verified_by="SIZE"
    )
    await store.amark_transfer_manifest_item_started(
        transfer.id, first_page[1].id, destination_existed_before=False
    )
    persisted_page = await store.alist_transfer_manifest_items(
        transfer.id, statuses={"PENDING"}, after_id=None, limit=200
    )
    counts = await store.aget_transfer_manifest_counts(transfer.id)
    current_source = await store.aget_connection(source.id)
    assert current_source is not None
    current_source.encrypted_credentials = b"rotated-source-credentials"
    await store.asave_connection(current_source)
    updated_source = await store.aget_connection(source.id)
    persisted_transfer = await store.aget_transfer(transfer.id)
    await store.aclose()

    assert inserted == 10_001
    assert repeated == 0
    assert len(first_page) == 200
    assert counts == {"total": 10_001, "pending": 10_000, "copied": 1, "skipped": 0, "failed": 0}
    assert updated_source is not None and updated_source.credential_version == 2
    assert persisted_transfer is not None
    assert persisted_transfer.source_credential_version == 1
    assert persisted_transfer.destination_credential_version == 1
    assert persisted_transfer.files_completed == 1
    assert persisted_transfer.bytes_transferred == first_page[0].size_bytes
    assert persisted_page[0].copy_started is True
    assert persisted_page[0].destination_existed_before is False


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
        idempotency_headers = {**headers, "Idempotency-Key": str(uuid.uuid4())}
        first = await client.post("/api/v1/transfers", json=payload, headers=idempotency_headers)
        second = await client.post("/api/v1/transfers", json=payload, headers=idempotency_headers)
        conflicting = await client.post(
            "/api/v1/transfers",
            json={**payload, "destination_path": "/Different"},
            headers=idempotency_headers,
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["public_id"] == second.json()["public_id"]
        assert conflicting.status_code == 409

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
async def test_terminal_transfer_and_notification_intent_commit_together(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'notification-outbox.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user("notify", "notify@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    async with store.sessions() as session:
        outbox_rows = (
            await session.execute(
                text(
                    "SELECT status, transfer_attempt FROM transfer_notification_outbox "
                    "WHERE transfer_job_id = :transfer_id"
                ),
                {"transfer_id": transfer.id.hex},
            )
        ).all()
    await store.aclose()

    assert outbox_rows == [("PENDING", 1)]


@pytest.mark.asyncio
async def test_legacy_notification_materialization_creates_one_durable_outbox_record(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-notification.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user("legacy-notify", "legacy@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)

    # Simulate a terminal transfer created before the outbox migration.
    async with store.sessions() as session:
        await session.execute(
            delete(TransferNotificationOutbox).where(
                TransferNotificationOutbox.transfer_job_id == transfer.id
            )
        )
    await store.aensure_transfer_notification_outbox(transfer.id)
    await store.aensure_transfer_notification_outbox(transfer.id)

    async with store.sessions() as session:
        rows = list(
            await session.scalars(
                select(TransferNotificationOutbox).where(
                    TransferNotificationOutbox.transfer_job_id == transfer.id
                )
            )
        )
    await store.aclose()

    assert len(rows) == 1
    assert rows[0].status == "PENDING"
    assert rows[0].snapshot["recipient"] == "legacy@example.test"


@pytest.mark.asyncio
async def test_notification_outbox_retries_from_postgres_state_and_hides_snapshot(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'notification-retries.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    user = await store.acreate_user("notify-retry", "private@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/private/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)

    now = datetime.now(UTC)
    dispatches = await store.adispatch_pending_notifications(now)
    assert len(dispatches) == 1
    notification_id, dispatch_token = dispatches[0]
    assert await store.adispatch_pending_notifications(now) == []
    delivery_claim = await store.aclaim_notification_delivery(notification_id, now, dispatch_token)
    assert delivery_claim is not None
    snapshot, claim_token = delivery_claim
    assert snapshot["recipient"] == user.email
    assert snapshot["source_name"] == "Source"
    assert await store.aclaim_notification_delivery(notification_id, now, dispatch_token) is None

    await store.afail_notification_delivery(
        notification_id, now, claim_token, "SMTP_DELIVERY_FAILED"
    )
    failed_attempt = (await store.alist_notification_outbox())[0]
    assert failed_attempt["status"] == "PENDING"
    assert failed_attempt["attempt_count"] == 1
    assert "recipient" not in failed_attempt

    retry_time = now + timedelta(seconds=20)
    retry_dispatches = await store.adispatch_pending_notifications(retry_time)
    assert [item[0] for item in retry_dispatches] == [notification_id]
    _, retry_dispatch_token = retry_dispatches[0]
    retry_claim = await store.aclaim_notification_delivery(
        notification_id, retry_time, retry_dispatch_token
    )
    assert retry_claim is not None
    _snapshot, retry_token = retry_claim
    await store.amark_notification_sent(notification_id, retry_token)
    delivered = (await store.alist_notification_outbox())[0]
    await store.aclose()

    assert delivered["status"] == "SENT"
    assert delivered["attempt_count"] == 2


@pytest.mark.asyncio
async def test_notification_delivery_lease_fences_stale_worker_completion(tmp_path):
    store = DatabaseStore(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'notification-lease.sqlite'}",
            session_cookie_secure=False,
        )
    )
    await store.create_schema()
    user = await store.acreate_user("lease-user", "lease@example.test", "hash")
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/file"], "/archive", send_email_notification=True
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    now = datetime.now(UTC)
    notification_id, first_dispatch_token = (await store.adispatch_pending_notifications(now))[0]
    first_claim = await store.aclaim_notification_delivery(
        notification_id, now, first_dispatch_token
    )
    assert first_claim is not None
    _snapshot, first_token = first_claim

    reclaimed_at = now + timedelta(seconds=301)
    reclaimed_dispatches = await store.adispatch_pending_notifications(reclaimed_at)
    assert [item[0] for item in reclaimed_dispatches] == [notification_id]
    _, second_dispatch_token = reclaimed_dispatches[0]
    assert (
        await store.aclaim_notification_delivery(
            notification_id, reclaimed_at, first_dispatch_token
        )
        is None
    )
    await store.arelease_notification_dispatch(notification_id, reclaimed_at, first_dispatch_token)
    second_claim = await store.aclaim_notification_delivery(
        notification_id, reclaimed_at, second_dispatch_token
    )
    assert second_claim is not None
    _snapshot, second_token = second_claim
    assert second_token != first_token

    await store.amark_notification_sent(notification_id, first_token)
    await store.afail_notification_delivery(
        notification_id, reclaimed_at, first_token, "SMTP_DELIVERY_FAILED"
    )
    still_sending = (await store.alist_notification_outbox())[0]
    assert still_sending["status"] == "SENDING"
    assert still_sending["attempt_count"] == 2

    await store.amark_notification_sent(notification_id, second_token)
    delivered = (await store.alist_notification_outbox())[0]
    await store.aclose()
    assert delivered["status"] == "SENT"


@pytest.mark.asyncio
async def test_database_store_archives_connection_and_keeps_transfer_history(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'archive.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    admin = await store.acreate_user("archive-admin", "archive-admin@example.test", "hash", "ADMIN")
    member = await store.acreate_user("archive-member", "archive-member@example.test", "hash")
    source = await store.acreate_connection(
        "Private source", "GOOGLE_DRIVE", admin.id, uuid.uuid4(), b"encrypted-secret", False
    )
    destination = await store.acreate_connection("Destination", "MOCK", admin.id, uuid.uuid4(), b"")
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
        await store.acreate_transfer(admin.id, source.id, destination.id, ["/new.pdf"], "/archive")
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
@pytest.mark.parametrize("status", ["QUEUED", "VALIDATING", "RUNNING", "RETRYING", "CANCELLING"])
async def test_database_store_rejects_archiving_connection_with_active_transfer(
    tmp_path, status: str
):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'{status}.sqlite'}",
        session_cookie_secure=False,
    )
    store = DatabaseStore(settings)
    await store.create_schema()
    admin = await store.acreate_user(f"admin-{status}", f"{status}@example.test", "hash", "ADMIN")
    source = await store.acreate_connection(
        "Source", "MOCK", admin.id, uuid.uuid4(), b"encrypted-secret"
    )
    destination = await store.acreate_connection("Destination", "MOCK", admin.id, uuid.uuid4(), b"")
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
    destination = await store.acreate_connection("Destination", "MOCK", admin.id, uuid.uuid4(), b"")
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


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
async def test_postgres_rejects_stale_progress_after_cancel_and_terminal_state():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"], session_cookie_secure=False)
    store = DatabaseStore(settings)
    assert store.engine.dialect.name == "postgresql"
    await store.create_schema()
    suffix = uuid.uuid4()
    user = await store.acreate_user(
        f"stale-save-{suffix}", f"stale-save-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id, source.id, destination.id, ["/file"], "/archive"
    )
    transfer.status = "RUNNING"
    await store.asave_transfer(transfer)
    stale_running = await store.aget_transfer(transfer.id)
    assert stale_running is not None

    cancelled, needs_worker = await store.acancel_transfer(transfer.id, datetime.now(UTC))
    assert cancelled is not None and needs_worker
    await store.asave_transfer(stale_running)
    persisted = await store.aget_transfer(transfer.id)
    assert persisted is not None and persisted.status == "CANCELLING"

    terminal = await store.aget_transfer(transfer.id)
    assert terminal is not None
    terminal.status = "COMPLETED"
    terminal.completed_at = datetime.now(UTC)
    await store.asave_transfer(terminal)
    await store.asave_transfer(stale_running)
    persisted = await store.aget_transfer(transfer.id)

    async with store.sessions() as session:
        await session.execute(delete(TransferJob).where(TransferJob.id == transfer.id))
        await session.execute(
            delete(StorageConnection).where(StorageConnection.id.in_([source.id, destination.id]))
        )
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
    await store.aclose()

    assert persisted is not None and persisted.status == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
async def test_postgres_serializes_concurrent_removal_of_active_admins():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"], session_cookie_secure=False)
    store = DatabaseStore(settings)
    assert store.engine.dialect.name == "postgresql"
    await store.create_schema()
    async with store.sessions() as session:
        previous_admins = list(
            (
                await session.scalars(
                    select(User)
                    .where(User.is_active.is_(True), User.role == "ADMIN")
                    .with_for_update()
                )
            ).all()
        )
        previous_admin_ids = [user.id for user in previous_admins]
        for user in previous_admins:
            user.role = "USER"
        await session.commit()
    suffix = uuid.uuid4()
    admins = [
        await store.acreate_user(
            f"admin-race-{suffix}-{index}",
            f"admin-race-{suffix}-{index}@example.test",
            "hash",
            "ADMIN",
        )
        for index in range(2)
    ]
    start = asyncio.Event()

    async def remove_admin(user_id: uuid.UUID):
        await start.wait()
        return await store.aupdate_user_profile(user_id, {"is_active": False})

    try:
        tasks = [asyncio.create_task(remove_admin(admin.id)) for admin in admins]
        start.set()
        results = await asyncio.gather(*tasks)
        active_admins = await store.acount_active_admins()
    finally:
        async with store.sessions() as session:
            await session.execute(delete(User).where(User.id.in_([admin.id for admin in admins])))
            previous_rows = list(
                (await session.scalars(select(User).where(User.id.in_(previous_admin_ids)))).all()
            )
            for user in previous_rows:
                user.role = "ADMIN"
            await session.commit()
    await store.aclose()

    assert sum(updated is not None for updated, _blocked in results) == 1
    assert sum(blocked for _updated, blocked in results) == 1
    assert active_admins == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
async def test_postgres_deduplicates_concurrent_transfer_creation_by_idempotency_key():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"], session_cookie_secure=False)
    store = DatabaseStore(settings)
    await store.create_schema()
    suffix = uuid.uuid4()
    user = await store.acreate_user(
        f"idempotency-{suffix}", f"idempotency-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    key = uuid.uuid4()
    request_hash = "a" * 64
    start = asyncio.Event()

    async def create_transfer():
        await start.wait()
        return await store.acreate_transfer(
            user.id,
            source.id,
            destination.id,
            ["/report.pdf"],
            "/archive",
            idempotency_key=key,
            idempotency_request_hash=request_hash,
        )

    try:
        tasks = [asyncio.create_task(create_transfer()) for _ in range(2)]
        start.set()
        first, second = await asyncio.gather(*tasks)
        assert first.id == second.id
        assert sum(record.idempotent_replay for record in (first, second)) == 1
    finally:
        async with store.sessions() as session:
            await session.execute(delete(TransferJob).where(TransferJob.idempotency_key == key))
            await session.execute(
                delete(StorageConnection).where(
                    StorageConnection.id.in_([source.id, destination.id])
                )
            )
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
    await store.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL integration DB")
async def test_postgres_notification_outbox_dispatch_and_delivery_claim_are_exclusive():
    settings = Settings(database_url=os.environ["TEST_DATABASE_URL"], session_cookie_secure=False)
    store = DatabaseStore(settings)
    assert store.engine.dialect.name == "postgresql"
    await store.create_schema()
    suffix = uuid.uuid4()
    user = await store.acreate_user(
        f"notification-race-{suffix}", f"notification-race-{suffix}@example.test", "hash"
    )
    source = await store.acreate_connection("Source", "MOCK", user.id, uuid.uuid4(), b"")
    destination = await store.acreate_connection("Destination", "MOCK", user.id, uuid.uuid4(), b"")
    transfer = await store.acreate_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "COMPLETED"
    transfer.completed_at = datetime.now(UTC)
    await store.asave_transfer(transfer)
    now = datetime.now(UTC)

    try:
        dispatch_results = await asyncio.gather(
            store.adispatch_pending_notifications(now),
            store.adispatch_pending_notifications(now),
        )
        dispatches = [dispatch for result in dispatch_results for dispatch in result]
        assert len(dispatches) == 1
        notification_id, dispatch_token = dispatches[0]

        claims = await asyncio.gather(
            store.aclaim_notification_delivery(notification_id, now, dispatch_token),
            store.aclaim_notification_delivery(notification_id, now, dispatch_token),
        )
        assert sum(snapshot is not None for snapshot in claims) == 1
    finally:
        async with store.sessions() as session:
            await session.execute(delete(TransferJob).where(TransferJob.id == transfer.id))
            await session.execute(
                delete(StorageConnection).where(
                    StorageConnection.id.in_([source.id, destination.id])
                )
            )
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
        await store.aclose()
