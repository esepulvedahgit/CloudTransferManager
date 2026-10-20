import asyncio
import json
import uuid
from dataclasses import replace

import httpx
import pytest

from app.main import create_app
from app.security.passwords import PasswordService
from app.services.event_bus import RedisEventBus
from app.services.memory_store import MemoryStore
from app.settings import Settings


@pytest.mark.asyncio
async def test_sse_reloads_transfer_after_subscribing_to_avoid_lost_terminal_event():
    store = MemoryStore()
    user = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    transfer = store.create_transfer(
        user.id, uuid.uuid4(), uuid.uuid4(), ["/source.txt"], "/backup"
    )
    await store.aadd_transfer_manifest_items(
        transfer.id,
        [
            {
                "source_path": "/source.txt",
                "destination_path": "/backup/source.txt",
                "size_bytes": 12,
            }
        ],
    )
    manifest_row = (await store.alist_transfer_manifest_items(transfer.id))[0]
    await store.aupdate_transfer_manifest_item(
        transfer.id, manifest_row.id, status="FAILED", error_code="SOURCE_ITEM_NOT_FOUND"
    )
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )

    get_transfer = store.aget_transfer
    first_read = True

    async def snapshot_before_race(transfer_id):
        nonlocal first_read
        current = await get_transfer(transfer_id)
        if first_read:
            first_read = False
            return replace(current, events=list(current.events)) if current else None
        return current

    store.aget_transfer = snapshot_before_race
    subscribe = app.state.event_hub.subscribe

    def complete_just_before_subscription(transfer_id):
        queue = subscribe(transfer_id)
        transfer.status = "COMPLETED"
        transfer.progress_percent = 100.0
        return queue

    app.state.event_hub.subscribe = complete_just_before_subscription

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        response = await asyncio.wait_for(
            client.get(f"/api/v1/transfers/{transfer.id}/stream"), timeout=1
        )

    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(frames) == 1
    assert '"status":"COMPLETED"' in frames[0]
    assert '"progress_percent":100.0' in frames[0]
    assert (
        '"manifest_summary":{"copied":0,"failed":1,"skipped":0,"pending":0,"renamed":0,"omitted":0,"omitted_bytes":0}'
        in frames[0]
    )


@pytest.mark.asyncio
async def test_redis_sse_waits_for_subscription_ack_and_keeps_events_during_snapshot():
    class PubSub:
        def __init__(self):
            self.channel = None
            self.ack_delivered = False
            self.subscribed = False
            self.messages = []

        async def subscribe(self, channel):
            self.channel = channel

        async def get_message(self, *, ignore_subscribe_messages=True, timeout=0):
            if not self.ack_delivered:
                self.ack_delivered = True
                self.subscribed = True
                if ignore_subscribe_messages:
                    return None
                return {"type": "subscribe", "channel": self.channel, "data": 1}
            if self.messages:
                return self.messages.pop(0)
            return None

        async def unsubscribe(self, _channel):
            return None

        async def aclose(self):
            return None

    class RedisClient:
        def __init__(self):
            self.subscription = PubSub()

        def pubsub(self):
            return self.subscription

    store = MemoryStore()
    user = store.add_user(
        "admin",
        "admin@example.test",
        PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    transfer = store.create_transfer(
        user.id, uuid.uuid4(), uuid.uuid4(), ["/source.txt"], "/backup"
    )
    transfer.status = "RUNNING"
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    redis = RedisClient()
    app.state.redis_client = redis
    app.state.event_bus = RedisEventBus(redis)

    get_transfer = store.aget_transfer
    read_count = 0

    async def transfer_read_started_before_terminal_publish(transfer_id):
        nonlocal read_count
        current = await get_transfer(transfer_id)
        read_count += 1
        if read_count == 1:
            return replace(current, events=list(current.events)) if current else None

        stale_snapshot = replace(current, events=list(current.events)) if current else None
        if current is not None:
            current.status = "COMPLETED"
            current.progress_percent = 100.0
            terminal_event = current.public_dict()
            terminal_event.update(
                {
                    "job_id": str(current.id),
                    "timestamp": "2026-10-10T10:15:32+00:00",
                }
            )
            if redis.subscription.subscribed:
                redis.subscription.messages.append(
                    {
                        "type": "message",
                        "channel": redis.subscription.channel,
                        "data": json.dumps(terminal_event),
                    }
                )
        return stale_snapshot

    store.aget_transfer = transfer_read_started_before_terminal_publish

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "admin", "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        response = await asyncio.wait_for(
            client.get(f"/api/v1/transfers/{transfer.id}/stream"), timeout=1
        )

    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(frames) == 2
    assert '"status":"RUNNING"' in frames[0]
    assert '"status":"COMPLETED"' in frames[1]


@pytest.mark.asyncio
async def test_sse_snapshot_includes_legacy_pause_reason_and_closes_at_paused():
    store = MemoryStore()
    user = store.add_user(
        "legacy-stream",
        "legacy-stream@example.test",
        PasswordService().hash("SafePassword123!"),
        role="ADMIN",
    )
    source = store.create_connection("MEGA origen", "MEGA", user.id)
    destination = store.create_connection("Drive destino", "GOOGLE_DRIVE", user.id)
    transfer = store.create_transfer(
        user.id, source.id, destination.id, ["/source.rar"], "/backup"
    )
    transfer.status = "RUNNING"
    transfer.conflict_policy = "SKIP"
    await store.asave_transfer(transfer)
    app = create_app(
        Settings(app_secret_key="test-secret-key", session_cookie_secure=False), store=store
    )
    subscribe = app.state.event_hub.subscribe

    def queue_paused_after_snapshot(transfer_id):
        queue = subscribe(transfer_id)
        queue.put_nowait({"status": "PAUSED", "job_id": str(transfer.id)})
        return queue

    app.state.event_hub.subscribe = queue_paused_after_snapshot

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        login = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": user.username, "password": "SafePassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert login.status_code == 200
        response = await asyncio.wait_for(
            client.get(f"/api/v1/transfers/{transfer.id}/stream"), timeout=1
        )

    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert frames[0]["pause_supported"] is False
    assert frames[0]["pause_unavailable_reason"] == "LEGACY_BULK_EXECUTION"
    assert frames[1]["status"] == "PAUSED"
