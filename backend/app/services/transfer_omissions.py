from __future__ import annotations

from enum import StrEnum


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
