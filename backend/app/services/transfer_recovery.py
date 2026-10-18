from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.services.memory_store import TransferRecord
from app.services.transfer_omissions import SourceFileErrorPolicy


class SkipAndResumeRequest(BaseModel):
    expected_attempt: int = Field(ge=1)
    request_id: uuid.UUID
    reason: Literal["USER_ACCEPTED_SOURCE_FAILURE"]
    source_file_error_policy: SourceFileErrorPolicy | None = None


class AcceptParentOmissionsRequest(BaseModel):
    expected_parent_attempt: int = Field(ge=1)
    request_id: uuid.UUID


class RetryOmittedRequest(BaseModel):
    expected_attempt: int = Field(ge=1)
    request_id: uuid.UUID
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("item_ids")
    @classmethod
    def item_ids_must_be_unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("item_ids must be unique")
        return value


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


async def accept_parent_omissions(
    store: Any,
    child_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    request: AcceptParentOmissionsRequest,
) -> TransferRecord:
    """Accept omissions from one specific successful parent attempt atomically."""
    return await store.aaccept_parent_omissions(
        child_id,
        actor_id=actor_id,
        expected_parent_attempt=request.expected_parent_attempt,
        request_id=request.request_id,
    )


async def retry_omitted_transfer_items(
    store: Any,
    transfer_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    request: RetryOmittedRequest,
) -> TransferRecord:
    """Requeue selected explicit omissions through the store's atomic unit of work."""
    return await store.aretry_omitted_transfer_items(
        transfer_id,
        item_ids=request.item_ids,
        actor_id=actor_id,
        expected_attempt=request.expected_attempt,
        request_id=request.request_id,
    )
