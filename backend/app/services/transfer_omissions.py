from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.memory_store import TransferManifestItemRecord, TransferRecord


class OmissionKind(StrEnum):
    USER_ACCEPTED_SOURCE_FAILURE = "USER_ACCEPTED_SOURCE_FAILURE"
    MEGA_FILE_RESTRICTED = "MEGA_FILE_RESTRICTED"


class SourceFileErrorPolicy(StrEnum):
    STOP = "STOP"
    SKIP_CONFIRMED_RESTRICTED = "SKIP_CONFIRMED_RESTRICTED"


def normalize_source_file_error_policy(value: str | SourceFileErrorPolicy) -> str:
    try:
        return SourceFileErrorPolicy(value).value
    except ValueError as exc:
        raise ValueError("Invalid source file error policy") from exc


class TransferRecoveryConflictError(ValueError):
    """A recovery action is stale or its safety conditions are not met."""


class TransferRecoveryNotFoundError(ValueError):
    """The transfer or manifest item is not visible to the actor."""


def skip_unavailable_reason(
    transfer: TransferRecord,
    item: TransferManifestItemRecord,
    *,
    failure_stage: str | None,
    failure_attempt: int | None,
) -> str | None:
    if transfer.source_provider_type != "MEGA":
        return "SOURCE_PROVIDER_UNSUPPORTED"
    if transfer.conflict_policy != "KEEP_BOTH":
        return "TRANSFER_NOT_RESUMABLE"
    if transfer.status != "FAILED":
        return "TRANSFER_NOT_FAILED"
    if item.is_dir:
        return "DIRECTORIES_CANNOT_BE_OMITTED"
    if item.status != "FAILED":
        return "ITEM_NOT_FAILED"
    if item.copy_started:
        return "DESTINATION_WRITE_MAY_HAVE_STARTED"
    code = (item.error_code or "").upper()
    if any(
        marker in code
        for marker in (
            "AUTH",
            "ACCOUNT",
            "QUOTA",
            "CLEANUP",
            "STAGING",
            "DESTINATION",
            "INTEGRITY",
            "LISTING_INVALID",
            "OUTPUT_INVALID",
            "SOURCE_ITEM_NOT_FOUND",
        )
    ):
        return "FAILURE_NOT_ELIGIBLE"
    if failure_stage != "SOURCE_DOWNLOAD":
        return "FAILURE_STAGE_UNKNOWN_OR_UNSUPPORTED"
    if failure_attempt is None or failure_attempt != transfer.attempt_count:
        return "FAILURE_ATTEMPT_STALE_OR_UNKNOWN"
    return None


def can_skip_manifest_item(
    transfer: TransferRecord,
    item: TransferManifestItemRecord,
    *,
    failure_stage: str | None,
    failure_attempt: int | None,
) -> bool:
    return (
        skip_unavailable_reason(
            transfer,
            item,
            failure_stage=failure_stage,
            failure_attempt=failure_attempt,
        )
        is None
    )


def resolve_failure_evidence(
    transfer: TransferRecord,
    item: TransferManifestItemRecord,
    events: list[dict[str, Any]],
    *,
    basename_matches: int,
) -> tuple[str | None, int | None]:
    """Resolve current-attempt per-file diagnostics without trusting raw provider output."""
    item_basename = PurePosixPath(item.source_path).name
    expected_id = str(item.id)
    for event in reversed(events):
        if event.get("event_type") != "FAILURE_DIAGNOSTIC":
            continue
        details = event.get("technical_details")
        if not isinstance(details, dict):
            continue
        event_item_id = details.get("manifest_item_id")
        if event_item_id is not None:
            if event_item_id != expected_id:
                continue
        elif basename_matches != 1 or details.get("file_name") != item_basename:
            continue
        if (event.get("provider_error_code") or "") != (item.error_code or ""):
            continue
        if details.get("provider") != "MEGA":
            continue
        if details.get("destination_write_started") is not False:
            continue
        failure_stage = details.get("provider_stage") or details.get("operation")
        if failure_stage != "SOURCE_DOWNLOAD":
            continue

        attempt = details.get("transfer_attempt") or details.get("attempt_count")
        if attempt is not None:
            if isinstance(attempt, bool) or not isinstance(attempt, int):
                continue
            if attempt != transfer.attempt_count:
                continue
        else:
            occurred_at = _parse_datetime(event.get("created_at"))
            started_at = _parse_datetime(transfer.started_at)
            if occurred_at is None or started_at is None or occurred_at < started_at:
                continue
            attempt = transfer.attempt_count

        if item.last_failure_stage is not None and item.last_failure_stage != failure_stage:
            continue
        if (
            item.last_failure_attempt is not None
            and item.last_failure_attempt != attempt
        ):
            continue
        return failure_stage, attempt
    return None, None


def recovery_request_hash(
    *, item_id: uuid.UUID, expected_attempt: int, source_file_error_policy: str | None
) -> str:
    payload = {
        "action": "skip-and-resume",
        "item_id": str(item_id),
        "expected_attempt": expected_attempt,
        "reason": OmissionKind.USER_ACCEPTED_SOURCE_FAILURE.value,
        "source_file_error_policy": source_file_error_policy,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parent_omissions_request_hash(*, child_id: uuid.UUID, expected_parent_attempt: int) -> str:
    payload = {
        "action": "accept-parent-omissions",
        "child_id": str(child_id),
        "expected_parent_attempt": expected_parent_attempt,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
