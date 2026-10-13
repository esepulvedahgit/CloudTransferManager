from dataclasses import dataclass
from typing import Any

from app.services.memory_store import TransferRecord

SUCCESSFUL_PARENT_STATUSES = frozenset({"COMPLETED", "COMPLETED_WITH_WARNINGS"})
FAILED_PARENT_STATUSES = frozenset({"FAILED", "CANCELLED"})


@dataclass(frozen=True)
class TransferReservation:
    transfer_id: Any
    source_connection_id: Any
    destination_connection_id: Any
    execution_mode: str
    uses_staging: bool


@dataclass
class DispatchBatch:
    admitted: list[TransferRecord]
    changed: list[TransferRecord]


def dependency_wait_reason(parent_status: str | None) -> str | None:
    if parent_status is None or parent_status in SUCCESSFUL_PARENT_STATUSES:
        return None
    if parent_status in FAILED_PARENT_STATUSES:
        return "BLOCKED_PARENT_FAILED"
    return "WAITING_PARENT"


def transfer_requires_staging(
    source_provider: str,
    destination_provider: str,
    source_adapter: str = "RCLONE",
    destination_adapter: str = "RCLONE",
) -> bool:
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
    active: list[TransferReservation],
    max_concurrent: int,
) -> str | None:
    if execution_mode == "EXCLUSIVE" and active:
        return "WAITING_EXCLUSIVE"
    if any(reservation.execution_mode == "EXCLUSIVE" for reservation in active):
        return "WAITING_EXCLUSIVE"
    if len(active) >= max_concurrent:
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
