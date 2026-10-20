import asyncio
import shutil
import smtplib
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.services.notifications as notifications
import app.worker as worker
from app.security.credentials import CredentialCipher
from app.services.memory_store import MemoryStore, SMTPSettingsRecord
from app.services.smtp_config import SMTPServerConfig
from app.services.staging import temporary_staging_area
from app.services.transfer_evaluation import EvaluationClaim
from app.settings import Settings
from app.worker import celery_app


def test_transfer_tasks_and_notifications_use_separate_worker_queues():
    routes = celery_app.conf.task_routes

    assert routes["app.worker.run_transfer"]["queue"] == "transfers"
    assert routes["app.worker.send_transfer_notification"]["queue"] == "notifications"
    assert routes["app.worker.dispatch_pending_notifications"]["queue"] == "control"
    assert celery_app.conf.task_default_queue == "transfers"
    assert worker.send_transfer_notification.max_retries is None

    assert celery_app.conf.broker_transport_options["visibility_timeout"] >= 24 * 60 * 60


def test_evaluation_tasks_use_a_dedicated_queue_and_beat_dispatch_cycle():
    assert celery_app.conf.task_routes["app.worker.run_transfer_evaluation"]["queue"] == (
        "evaluations"
    )
    assert celery_app.conf.task_routes["app.worker.dispatch_pending_evaluations"]["queue"] == (
        "control"
    )
    assert celery_app.conf.beat_schedule["dispatch-pending-evaluations"]["schedule"] == 15.0
    assert (
        celery_app.conf.beat_schedule["dispatch-pending-evaluations"]["options"]["queue"]
        == "control"
    )


@pytest.mark.asyncio
async def test_evaluation_dispatcher_claims_then_publishes_to_evaluation_queue(monkeypatch):
    transfer_id = uuid.uuid4()
    claim = EvaluationClaim(transfer_id, 4, uuid.uuid4())
    published = []

    class EvaluationStore:
        async def aclaim_due_evaluations(self, _now, *, limit):
            assert limit == 20
            return [claim]

        async def aclose(self):
            return None

    class EvaluationTask:
        def apply_async(self, *, args, queue):
            published.append((args, queue))

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: EvaluationStore())
    monkeypatch.setattr(worker, "run_transfer_evaluation", EvaluationTask())

    dispatched = await worker._dispatch_pending_evaluations()

    assert dispatched == 1
    assert published == [
        ([str(transfer_id), 4, str(claim.claim_token)], "evaluations")
    ]


def test_persistent_dispatcher_runs_on_a_separate_control_queue_every_15_seconds():
    assert (
        celery_app.conf.task_routes["app.worker.dispatch_pending_transfers"]["queue"]
        == "control"
    )
    assert celery_app.conf.beat_schedule["dispatch-pending-transfers"]["schedule"] == 15.0
    assert (
        celery_app.conf.beat_schedule["dispatch-pending-transfers"]["options"]["queue"]
        == "control"
    )
    assert celery_app.conf.task_routes["app.worker.record_beat_heartbeat"]["queue"] == "control"
    assert celery_app.conf.beat_schedule["beat-health-heartbeat"]["schedule"] == 15.0
    assert celery_app.conf.beat_schedule["dispatch-pending-notifications"]["schedule"] == 15.0
    assert celery_app.conf.task_routes["app.worker.cleanup_orphaned_staging_areas"]["queue"] == (
        "transfers"
    )
    assert celery_app.conf.beat_schedule["cleanup-orphaned-staging-areas"]["schedule"] == 600.0


def test_legacy_notification_task_messages_remain_accepted_during_outbox_cutover(monkeypatch):
    transfer_id = uuid.uuid4()
    materialized = []

    async def materialize_legacy_notification(legacy_transfer_id):
        materialized.append(legacy_transfer_id)

    monkeypatch.setattr(
        worker,
        "_materialize_legacy_notification",
        materialize_legacy_notification,
        raising=False,
    )

    worker.send_transfer_notification.run(str(transfer_id))

    assert materialized == [transfer_id]
    assert (
        celery_app.conf.beat_schedule["dispatch-pending-notifications"]["options"]["queue"]
        == "control"
    )


@pytest.mark.asyncio
async def test_redelivered_transfer_does_not_run_or_clear_active_cancellation(monkeypatch):
    transfer_id = uuid.uuid4()
    dispatch_token = uuid.uuid4()
    calls: list[str] = []

    class LockedStore:
        @asynccontextmanager
        async def transfer_execution_lock(self, supplied_id):
            calls.append(f"lock:{supplied_id}")
            yield False

        async def arelease_transfer_dispatch(self, supplied_id, supplied_token):
            calls.append(f"release:{supplied_id}:{supplied_token}")

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
    await worker._execute_transfer(transfer_id, dispatch_token)

    assert calls == [
        f"lock:{transfer_id}",
        f"release:{transfer_id}:{dispatch_token}",
        "event_bus_closed",
        "store_closed",
    ]


@pytest.mark.asyncio
async def test_progress_publish_failure_does_not_fail_a_completed_transfer(monkeypatch):
    reservation = {"active": False, "active_when_completed_was_published": None}

    class WorkerStore(MemoryStore):
        @asynccontextmanager
        async def transfer_execution_lock(self, _transfer_id):
            yield True

        async def astart_transfer_dispatch(self, transfer_id, token, now):
            reservation["active"] = True
            transfer = self.transfers[transfer_id]
            transfer.dispatch_token = token
            transfer.execution_started_at = now
            return True

        async def afinish_transfer_dispatch(self, _transfer_id, _token):
            assert reservation["active"] is True
            reservation["active"] = False
            return None

        async def afail_transfer_dispatch(self, transfer_id, _token, **_kwargs):
            self.transfers[transfer_id].status = "FAILED"

        async def aclose(self):
            return None

    class EventBus:
        def __init__(self, _client):
            pass

        async def publish_progress(self, _transfer_id, event):
            if event["status"] == "COMPLETED":
                reservation["active_when_completed_was_published"] = reservation["active"]
                raise ConnectionError("Redis is unavailable")

        async def clear_cancel(self, _transfer_id):
            return None

        async def aclose(self):
            return None

    store = WorkerStore()
    user = store.add_user("worker-owner", "worker-owner@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/Proyectos"], "/Backup")
    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "RedisEventBus", EventBus)

    await worker._execute_transfer(transfer.id, uuid.uuid4())

    assert (await store.aget_transfer(transfer.id)).status == "COMPLETED"
    assert reservation["active_when_completed_was_published"] is True
    assert reservation["active"] is False


@pytest.mark.asyncio
async def test_worker_does_not_publish_progress_after_dispatch_token_is_replaced(monkeypatch):
    tokens = {"replacement": None}
    published_events = []

    class WorkerStore(MemoryStore):
        @asynccontextmanager
        async def transfer_execution_lock(self, _transfer_id):
            yield True

        async def astart_transfer_dispatch(self, transfer_id, token, now):
            transfer = self.transfers[transfer_id]
            transfer.dispatch_token = token
            transfer.execution_started_at = now
            return True

        async def afinish_transfer_dispatch(self, transfer_id, token):
            transfer = self.transfers[transfer_id]
            if transfer.dispatch_token == token:
                transfer.dispatch_token = None
                transfer.execution_started_at = None

        async def aclose(self):
            return None

    class EventBus:
        def __init__(self, _client):
            pass

        async def publish_progress(self, _transfer_id, event):
            published_events.append(event)

        async def clear_cancel(self, _transfer_id):
            return None

        async def aclose(self):
            return None

    class StaleExecutor:
        def __init__(self, _settings, store, publish, _should_cancel, **_kwargs):
            self.store = store
            self.publish = publish

        async def execute(self, transfer_id):
            stale = replace(await self.store.aget_transfer(transfer_id))
            stale.status = "RUNNING"
            stale.bytes_transferred = 500
            replacement = uuid.uuid4()
            self.store.transfers[transfer_id].dispatch_token = replacement
            tokens["replacement"] = replacement
            await self.publish(stale)
            return False

    store = WorkerStore()
    user = store.add_user("stale-worker", "stale-worker@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/source.txt"], "/dest")
    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "RedisEventBus", EventBus)
    monkeypatch.setattr(worker, "TransferExecutor", StaleExecutor)

    await worker._execute_transfer(transfer.id, uuid.uuid4())

    current = await store.aget_transfer(transfer.id)
    assert published_events == []
    assert current.status == "QUEUED"
    assert current.bytes_transferred == 0
    assert current.dispatch_token == tokens["replacement"]


@pytest.mark.asyncio
async def test_worker_keeps_reservation_until_failed_staging_cleanup_finishes(
    monkeypatch, tmp_path
):
    reservation = {"active": False, "active_during_failure": None, "active_at_finish": None}
    settings = Settings(transfer_staging_path=str(tmp_path / "staging"))

    class WorkerStore(MemoryStore):
        @asynccontextmanager
        async def transfer_execution_lock(self, _transfer_id):
            yield True

        async def astart_transfer_dispatch(self, transfer_id, token, now):
            reservation["active"] = True
            transfer = self.transfers[transfer_id]
            transfer.dispatch_token = token
            transfer.execution_started_at = now
            return True

        async def afail_transfer_dispatch(self, transfer_id, _token, **_kwargs):
            transfer = self.transfers[transfer_id]
            reservation["active_during_failure"] = reservation["active"]
            assert transfer.execution_started_at is not None
            transfer.status = "FAILED"

        async def afinish_transfer_dispatch(self, transfer_id, _token):
            transfer = self.transfers[transfer_id]
            reservation["active_at_finish"] = reservation["active"]
            reservation["active"] = False
            transfer.dispatch_token = None
            transfer.execution_started_at = None

        async def aclose(self):
            return None

    class EventBus:
        def __init__(self, _client):
            pass

        async def clear_cancel(self, _transfer_id):
            return None

        async def aclose(self):
            return None

    class CleanupFailingExecutor:
        def __init__(self, current_settings, _store, _publish, _should_cancel, **_kwargs):
            self.settings = current_settings

        async def execute(self, transfer_id):
            async with temporary_staging_area(
                Path(self.settings.transfer_staging_path),
                transfer_id=transfer_id,
                expected_size=None,
            ):
                pass

    store = WorkerStore(settings)
    user = store.add_user("cleanup-owner", "cleanup@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/source"], "/dest")
    real_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith("transfer-"):
            raise OSError("staging cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "RedisEventBus", EventBus)
    monkeypatch.setattr(worker, "TransferExecutor", CleanupFailingExecutor)
    monkeypatch.setattr("app.services.staging.shutil.rmtree", fail_cleanup)

    await worker._execute_transfer(transfer.id, uuid.uuid4())

    monkeypatch.setattr("app.services.staging.shutil.rmtree", real_rmtree)
    assert reservation == {
        "active": False,
        "active_during_failure": True,
        "active_at_finish": True,
    }
    assert (await store.aget_transfer(transfer.id)).status == "FAILED"
    leftover_areas = list(Path(settings.transfer_staging_path).iterdir())
    assert len(leftover_areas) == 1
    real_rmtree(leftover_areas[0])


@pytest.mark.asyncio
async def test_worker_stops_transfer_when_dispatch_heartbeat_is_lost(monkeypatch):
    class LostOwnershipStore:
        def __init__(self):
            self.failed_dispatches = 0

        @asynccontextmanager
        async def transfer_execution_lock(self, _transfer_id):
            yield True

        async def astart_transfer_dispatch(self, _transfer_id, _token, _now):
            return True

        async def aheartbeat_transfer_dispatch(self, _transfer_id, _token, _now):
            return False

        async def ais_transfer_cancel_requested(self, _transfer_id):
            return False

        async def afail_transfer_dispatch(self, _transfer_id, _token, **_kwargs):
            self.failed_dispatches += 1

        async def afinish_transfer_dispatch(self, _transfer_id, _token):
            return None

        async def aclose(self):
            return None

    class EventBus:
        def __init__(self, _client):
            pass

        async def clear_cancel(self, _transfer_id):
            return None

        async def aclose(self):
            return None

    class LongRunningExecutor:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, _transfer_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                raise

    store = LostOwnershipStore()
    cancellation_seen = asyncio.Event()
    original_sleep = asyncio.sleep

    async def accelerated_sleep(delay):
        await original_sleep(0.02 if delay == 10 else delay)

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "RedisEventBus", EventBus)
    monkeypatch.setattr(worker, "TransferExecutor", LongRunningExecutor)
    monkeypatch.setattr(worker.asyncio, "sleep", accelerated_sleep)

    await asyncio.wait_for(worker._execute_transfer(uuid.uuid4(), uuid.uuid4()), timeout=1)

    assert cancellation_seen.is_set()
    assert store.failed_dispatches == 1


@pytest.mark.asyncio
async def test_notification_dispatcher_publishes_outbox_id_to_isolated_queue(monkeypatch):
    store = MemoryStore()
    user = store.add_user("notify-dispatch", "dispatch@example.test", "hash")
    source = store.create_connection("Source", "MOCK", user.id)
    destination = store.create_connection("Destination", "MOCK", user.id)
    transfer = store.create_transfer(
        user.id,
        source.id,
        destination.id,
        ["/report.pdf"],
        "/archive",
        send_email_notification=True,
    )
    transfer.status = "FAILED"
    await store.asave_transfer(transfer)

    async def close_store():
        return None

    store.aclose = close_store
    enqueued = []

    class NotificationTask:
        def apply_async(self, *, args, queue):
            enqueued.append((args, queue))

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: store)
    monkeypatch.setattr(worker, "send_transfer_notification", NotificationTask())

    assert await worker._dispatch_pending_notifications() == 1
    notification_id = next(iter(store.notification_outbox))
    dispatch_token = store.notification_outbox[notification_id]["claim_token"]
    assert enqueued == [([str(notification_id), str(dispatch_token)], "notifications")]
    assert store.notification_outbox[notification_id]["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_staging_sweep_uses_active_transfer_ids_and_configured_path(
    monkeypatch, tmp_path
):
    active_ids = {uuid.uuid4()}
    settings = Settings(transfer_staging_path=str(tmp_path))
    captured = {}

    class StagingStore:
        async def alist_active_transfer_ids(self):
            return active_ids

        async def aclose(self):
            return None

    def cleanup(root, *, active_transfer_ids):
        captured["root"] = root
        captured["active_transfer_ids"] = active_transfer_ids
        return 3

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "DatabaseStore", lambda _settings: StagingStore())
    monkeypatch.setattr(worker, "cleanup_staging_areas", cleanup)

    assert await worker._cleanup_orphaned_staging_areas() == 3
    assert captured["root"].samefile(tmp_path)
    assert captured["active_transfer_ids"] == active_ids


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
    await transfer_store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/private/restricted.rar",
                "destination_path": "/archive/restricted.rar",
                "size_bytes": 686_289_042,
            }
        ],
    )
    omitted = (await transfer_store.alist_transfer_manifest_items(transfer.id))[0]
    omitted.status = "SKIPPED"
    omitted.omission_kind = "USER_ACCEPTED_SOURCE_FAILURE"
    transfer.status = "COMPLETED_WITH_WARNINGS"
    await transfer_store.asave_transfer(transfer)
    transfer.attempt_count += 1
    transfer.status = "QUEUED"
    omitted.status = "PENDING"
    omitted.omission_kind = None
    await transfer_store.asave_transfer(transfer)
    notification_id, dispatch_token = (
        await transfer_store.adispatch_pending_notifications(datetime.now(UTC))
    )[0]

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

    await worker._send_notification(notification_id, dispatch_token)

    assert captured["args"][0] is settings
    assert captured["args"][1] == resolved
    assert captured["args"][2] == user.email
    assert captured["args"][3].public_id == transfer.public_id
    assert captured["args"][3].omitted == 1
    assert captured["args"][3].omitted_bytes == 686_289_042
    snapshot = transfer_store.notification_outbox[notification_id]["snapshot"]
    assert "restricted.rar" not in str(snapshot)
    assert transfer_store.notification_outbox[notification_id]["status"] == "SENT"


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
    await store.asave_transfer(transfer)
    notification_id, dispatch_token = (
        await store.adispatch_pending_notifications(datetime.now(UTC))
    )[0]
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

    await worker._send_notification(notification_id, dispatch_token)

    assert captured["connection"] == ("saved.smtp.test", 2525, 20)
    assert captured["credentials"] == ("saved-smtp-user", password)
    assert password not in str(store.notification_outbox[notification_id])
    assert store.notification_outbox[notification_id]["status"] == "PENDING"
    assert store.notification_outbox[notification_id]["last_error_code"] == "SMTP_DELIVERY_FAILED"
    assert transfer.status == "COMPLETED"
