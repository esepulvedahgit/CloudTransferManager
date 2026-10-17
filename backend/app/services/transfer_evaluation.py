import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class EvaluationConflictError(ValueError):
    """Raised when a transfer cannot start another evaluation generation."""


@dataclass(frozen=True)
class EvaluationClaim:
    transfer_id: uuid.UUID
    generation: int
    claim_token: uuid.UUID


@dataclass
class EvaluationRecord:
    """Durable source inventory evaluation state for a scheduled transfer."""

    id: uuid.UUID
    transfer_id: uuid.UUID
    status: str = "PENDING"
    generation: int = 1
    sequence: int = 0
    files_found: int | None = None
    count_complete: bool = False
    max_files: int | None = None
    source_credential_version: int | None = None
    destination_credential_version: int | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    safe_error_code: str | None = None
    safe_message: str | None = None
    attempt_count: int = 0
    retry_at: datetime | None = None
    claim_token: uuid.UUID | None = None
    heartbeat_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> "EvaluationRecord":
        return cls(
            id=row.id,
            transfer_id=row.transfer_job_id,
            status=row.status,
            generation=row.generation,
            sequence=row.sequence,
            files_found=row.files_found,
            count_complete=row.count_complete,
            max_files=row.max_files,
            source_credential_version=row.source_credential_version,
            destination_credential_version=row.destination_credential_version,
            started_at=row.started_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
            safe_error_code=row.safe_error_code,
            safe_message=row.safe_message,
            attempt_count=row.attempt_count,
            retry_at=row.retry_at,
            claim_token=row.claim_token,
            heartbeat_at=row.heartbeat_at,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generation": self.generation,
            "sequence": self.sequence,
            "files_found": self.files_found,
            "count_complete": self.count_complete,
            "max_files": self.max_files,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "safe_error_code": self.safe_error_code,
            "safe_message": self.safe_message,
        }


def not_required_evaluation() -> dict[str, Any]:
    """Public compatibility value for transfers without an evaluation row."""
    return {
        "status": "NOT_REQUIRED",
        "generation": 0,
        "sequence": 0,
        "files_found": None,
        "count_complete": False,
        "max_files": None,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "safe_error_code": None,
        "safe_message": None,
    }


def restart_evaluation(
    evaluation: Any,
    *,
    now: datetime,
    max_files: int | None = None,
) -> None:
    """Invalidate previous count and claims while starting a fresh generation."""
    evaluation.generation += 1
    evaluation.sequence = 0
    evaluation.status = "PENDING"
    evaluation.files_found = None
    evaluation.count_complete = False
    if max_files is not None:
        evaluation.max_files = max_files
    evaluation.started_at = None
    evaluation.updated_at = now
    evaluation.completed_at = None
    evaluation.safe_error_code = None
    evaluation.safe_message = None
    evaluation.attempt_count = 0
    evaluation.retry_at = None
    evaluation.claim_token = None
    evaluation.heartbeat_at = None
