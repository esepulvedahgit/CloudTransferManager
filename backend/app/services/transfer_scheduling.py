from dataclasses import dataclass
from typing import Any

from app.services.memory_store import TransferRecord
from app.services.transfer_evaluation import EvaluationRecord

SUCCESSFUL_PARENT_STATUSES = frozenset({"COMPLETED", "COMPLETED_WITH_WARNINGS"})
FAILED_PARENT_STATUSES = frozenset({"FAILED", "CANCELLED"})


@dataclass(frozen=True)
class TransferReservation:
    transfer_id: Any
    source_connection_id: Any
    destination_connection_id: Any
    execution_mode: str
    uses_staging: bool
    uses_nextcloud: bool = False


@dataclass
class DispatchBatch:
    admitted: list[TransferRecord]
    changed: list[TransferRecord]


def dependency_wait_reason(
    parent_status: str | None,
    *,
    parent_omitted_count: int = 0,
    parent_attempt_count: int = 1,
    accepted_parent_omissions_attempt: int | None = None,
) -> str | None:
    if parent_status is None:
        return None
    if parent_status in FAILED_PARENT_STATUSES:
        return "BLOCKED_PARENT_FAILED"
    if parent_status not in SUCCESSFUL_PARENT_STATUSES:
        return "WAITING_PARENT"
    if (
        parent_omitted_count > 0
        and accepted_parent_omissions_attempt != parent_attempt_count
    ):
        return "BLOCKED_PARENT_OMISSIONS"
    return None


def dependency_error_code(wait_reason: str | None) -> str | None:
    return (
        wait_reason
        if wait_reason in {"BLOCKED_PARENT_FAILED", "BLOCKED_PARENT_OMISSIONS"}
        else None
    )


def dependency_wait_message(wait_reason: str | None) -> str:
    if wait_reason == "BLOCKED_PARENT_OMISSIONS":
        return (
            "La transferencia padre terminó con archivos omitidos; "
            "acepta el intento vigente antes de continuar."
        )
    if wait_reason == "BLOCKED_PARENT_FAILED":
        return "La transferencia padre debe completarse correctamente antes de continuar."
    return "La selección se evaluará cuando termine la transferencia padre."


def evaluation_wait_reason(evaluation: EvaluationRecord | None) -> str | None:
    if evaluation is None or evaluation.status in {"APPROVED", "NOT_REQUIRED"}:
        return None
    if evaluation.status in {"PENDING", "EVALUATING"}:
        return "WAITING_EVALUATION"
    if evaluation.status in {"BLOCKED", "ERROR"}:
        return "BLOCKED_EVALUATION"
    if evaluation.status == "WAITING_PARENT":
        return "WAITING_PARENT"
    return "WAITING_EVALUATION"


def transfer_requires_staging(
    source_provider: str,
    destination_provider: str,
    source_adapter: str = "RCLONE",
    destination_adapter: str = "RCLONE",
    conflict_policy: str = "KEEP_BOTH",
) -> bool:
    if conflict_policy == "KEEP_BOTH":
        return True
    return (
        source_provider in {"SMB", "MEGA"}
        or destination_provider == "MEGA"
        or (source_provider == "SEAFILE" and source_adapter == "SEAFILE_API")
        or (destination_provider == "SEAFILE" and destination_adapter == "SEAFILE_API")
    )


def admission_wait_reason(
    *,
    source_connection_id: Any,
    destination_connection_id: Any,
    execution_mode: str,
    uses_staging: bool,
    uses_nextcloud: bool = False,
    active: list[TransferReservation],
    max_concurrent: int,
    max_nextcloud_concurrent: int = 1,
) -> str | None:
    if execution_mode == "EXCLUSIVE" and active:
        return "WAITING_EXCLUSIVE"
    if any(reservation.execution_mode == "EXCLUSIVE" for reservation in active):
        return "WAITING_EXCLUSIVE"
    if len(active) >= max_concurrent:
        return "WAITING_CAPACITY"
    active_nextcloud = sum(reservation.uses_nextcloud for reservation in active)
    if uses_nextcloud and active_nextcloud >= max_nextcloud_concurrent:
        return "WAITING_CAPACITY"

    for reservation in active:
        if (
            source_connection_id == reservation.destination_connection_id
            or destination_connection_id
            in {reservation.source_connection_id, reservation.destination_connection_id}
        ):
            return "WAITING_CONNECTION"
    if uses_staging and any(reservation.uses_staging for reservation in active):
        return "WAITING_STAGING"
    return None
