import uuid
from typing import Any

from app.security.sanitize import sanitize_data


class AuditLogger:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def record(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        result: str,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await self.store.aadd_audit_event(
            actor_user_id=actor_user_id,
            action=action[:64],
            resource_type=resource_type[:64],
            resource_id=resource_id[:64] if resource_id else None,
            result=result,
            metadata=sanitize_data(metadata or {}),
            ip_address=ip_address,
            user_agent=user_agent,
        )
