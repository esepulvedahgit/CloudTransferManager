import json

import pytest

from app.services.event_bus import RedisEventBus


class FakeRedis:
    def __init__(self):
        self.messages = []
        self.keys = {}

    async def publish(self, channel, message):
        self.messages.append((channel, message))
        return 1

    async def set(self, key, value, ex=None):
        self.keys[key] = (value, ex)

    async def exists(self, key):
        return key in self.keys

    async def delete(self, key):
        self.keys.pop(key, None)

    async def ping(self):
        return True


@pytest.mark.asyncio
async def test_redis_event_bus_publishes_progress_and_tracks_cooperative_cancellation():
    client = FakeRedis()
    bus = RedisEventBus(client)
    await bus.publish_progress(
        "transfer-id",
        {"status": "RUNNING", "bytes_transferred": 4, "refresh_token": "must-not-leak"},
    )
    channel, message = client.messages[0]
    assert channel == "transfer:transfer-id:progress"
    assert json.loads(message)["bytes_transferred"] == 4
    assert json.loads(message)["refresh_token"] == "[REDACTED]"
    assert await bus.cancellation_requested("transfer-id") is False
    await bus.request_cancel("transfer-id")
    assert await bus.cancellation_requested("transfer-id") is True
    await bus.clear_cancel("transfer-id")
    assert await bus.cancellation_requested("transfer-id") is False


@pytest.mark.asyncio
async def test_beat_heartbeat_expires_when_periodic_control_work_stops():
    client = FakeRedis()
    bus = RedisEventBus(client)

    assert await bus.beat_is_alive() is False
    await bus.record_beat_heartbeat()
    assert await bus.beat_is_alive() is True
    assert client.keys["ctm:health:beat-heartbeat"] == ("1", 60)
