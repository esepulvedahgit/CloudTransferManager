from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.memory_store import TransferRecord
from app.services.transfer_omissions import SourceFileErrorPolicy


class SkipAndResumeRequest(BaseModel):
    expected_attempt: int = Field(ge=1)
    request_id: uuid.UUID
    reason: Literal["USER_ACCEPTED_SOURCE_FAILURE"]
    source_file_error_policy: SourceFileErrorPolicy | None = None


async def skip_and_resume_transfer(
    store: Any,
    transfer_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    request: SkipAndResumeRequest,
) -> TransferRecord:
    """Run the audited state transition through the store's atomic unit of work."""
    return await store.askip_transfer_item_and_resume(
        transfer_id,
        item_id,
        actor_id=actor_id,
        expected_attempt=request.expected_attempt,
        request_id=request.request_id,
        source_file_error_policy=(
            request.source_file_error_policy.value
            if request.source_file_error_policy is not None
            else None
        ),
    )
