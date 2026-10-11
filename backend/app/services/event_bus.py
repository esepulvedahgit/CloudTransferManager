import json
from typing import Any

from app.security.sanitize import sanitize_data


class RedisEventBus:
    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def progress_channel(transfer_id: str) -> str:
        return f"transfer:{transfer_id}:progress"

    @staticmethod
    def cancellation_key(transfer_id: str) -> str:
        return f"ctm:transfer:{transfer_id}:cancel"

    async def publish_progress(self, transfer_id: str, event: dict[str, Any]) -> None:
        payload = sanitize_data(event)
        await self.client.publish(
            self.progress_channel(transfer_id),
            json.dumps(payload, separators=(",", ":")),
        )

    async def request_cancel(self, transfer_id: str) -> None:
        await self.client.set(self.cancellation_key(transfer_id), "1", ex=3600)

    async def cancellation_requested(self, transfer_id: str) -> bool:
        return bool(await self.client.exists(self.cancellation_key(transfer_id)))

    async def clear_cancel(self, transfer_id: str) -> None:
        await self.client.delete(self.cancellation_key(transfer_id))

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()
