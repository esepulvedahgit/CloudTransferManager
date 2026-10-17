from dataclasses import dataclass
from enum import StrEnum


class CopyOutcome(StrEnum):
    COPIED = "COPIED"
    FAILED_SAFE = "FAILED_SAFE"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class FileCopyResult:
    outcome: CopyOutcome
    error_code: str | None = None
    provider_stage: str | None = None


class SourceFileError(Exception):
    """An isolated source failure, raised only before any destination write."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
