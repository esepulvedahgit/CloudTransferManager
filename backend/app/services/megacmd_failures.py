from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.services.transfer_omissions import SourceFileErrorPolicy


class MegaFailureCategory(StrEnum):
    RESOURCE_RESTRICTED = "RESOURCE_RESTRICTED"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
    AUTHENTICATION = "AUTHENTICATION"
    QUOTA = "QUOTA"
    LOCAL_IO = "LOCAL_IO"
    TRANSIENT = "TRANSIENT"
    UNKNOWN = "UNKNOWN"


class MegaFailureScope(StrEnum):
    FILE = "FILE"
    ACCOUNT = "ACCOUNT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MegaFailureDiagnostic:
    category: MegaFailureCategory
    scope: MegaFailureScope
    retryable: bool
    safe_code: str


def classify_megacmd_failure(return_code: int, output: bytes) -> MegaFailureDiagnostic:
    """Keep MEGAcmd errors unclassified until a version-matched CLI signature is verified.

    MEGAcmd's process exit status and MEGA SDK error codes do not share a documented,
    version-stable mapping in this installation. The output may also contain private
    paths and provider metadata, so this function deliberately never retains it.
    """
    del return_code, output
    return MegaFailureDiagnostic(
        category=MegaFailureCategory.UNKNOWN,
        scope=MegaFailureScope.UNKNOWN,
        retryable=False,
        safe_code="MEGACMD_FAILURE_UNCLASSIFIED",
    )


def can_auto_omit_mega_failure(
    policy: str | SourceFileErrorPolicy,
    diagnostic: MegaFailureDiagnostic,
    *,
    provider: str,
    provider_stage: str,
    copy_started: bool,
    cleanup_succeeded: bool,
) -> bool:
    try:
        selected_policy = SourceFileErrorPolicy(policy)
    except (TypeError, ValueError):
        return False
    return (
        selected_policy == SourceFileErrorPolicy.SKIP_CONFIRMED_RESTRICTED
        and diagnostic.category == MegaFailureCategory.RESOURCE_RESTRICTED
        and diagnostic.scope == MegaFailureScope.FILE
        and provider == "MEGA"
        and provider_stage == "SOURCE_DOWNLOAD"
        and not copy_started
        and cleanup_succeeded
    )
