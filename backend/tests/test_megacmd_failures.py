import pytest

from app.services.megacmd_failures import (
    MegaFailureCategory,
    MegaFailureDiagnostic,
    MegaFailureScope,
    can_auto_omit_mega_failure,
    classify_megacmd_failure,
)
from app.services.transfer_omissions import SourceFileErrorPolicy


@pytest.mark.parametrize(
    ("return_code", "output"),
    [
        (16, b"API error 16"),
        (16, b"resource administratively blocked"),
        (1, b"API_EBLOCKED"),
        (1, b"account is blocked"),
        (1, b"API_EOVERQUOTA"),
        (1, b"authentication failed"),
        (1, b"temporarily unavailable"),
        (1, b"No space left on device"),
        (1, b"an unrecognized provider response"),
    ],
)
def test_unverified_megacmd_signatures_remain_unknown_and_not_auto_omittable(
    return_code: int, output: bytes
):
    diagnostic = classify_megacmd_failure(return_code, output)

    assert diagnostic.category == MegaFailureCategory.UNKNOWN
    assert diagnostic.scope == MegaFailureScope.UNKNOWN
    assert diagnostic.safe_code == "MEGACMD_FAILURE_UNCLASSIFIED"
    assert not can_auto_omit_mega_failure(
        SourceFileErrorPolicy.SKIP_CONFIRMED_RESTRICTED,
        diagnostic,
        provider="MEGA",
        provider_stage="SOURCE_DOWNLOAD",
        copy_started=False,
        cleanup_succeeded=True,
    )


def test_diagnostic_does_not_retain_raw_paths_or_output():
    secretish_output = b"API error for /private/folder/restricted.rar: blocked"

    diagnostic = classify_megacmd_failure(1, secretish_output)

    assert diagnostic.safe_code == "MEGACMD_FAILURE_UNCLASSIFIED"
    assert "/private/folder" not in repr(diagnostic)
    assert "restricted.rar" not in repr(diagnostic)


def test_auto_omission_gate_requires_a_file_scoped_mega_source_download_before_write():
    verified_shape = MegaFailureDiagnostic(
        category=MegaFailureCategory.RESOURCE_RESTRICTED,
        scope=MegaFailureScope.FILE,
        retryable=False,
        safe_code="MEGA_FILE_RESTRICTED",
    )

    assert can_auto_omit_mega_failure(
        SourceFileErrorPolicy.SKIP_CONFIRMED_RESTRICTED,
        verified_shape,
        provider="MEGA",
        provider_stage="SOURCE_DOWNLOAD",
        copy_started=False,
        cleanup_succeeded=True,
    )
    assert not can_auto_omit_mega_failure(
        SourceFileErrorPolicy.STOP,
        verified_shape,
        provider="MEGA",
        provider_stage="SOURCE_DOWNLOAD",
        copy_started=False,
        cleanup_succeeded=True,
    )


@pytest.mark.parametrize(
    ("provider", "provider_stage", "copy_started", "cleanup_succeeded"),
    [
        ("MEGA", "SOURCE_DOWNLOAD", True, True),
        ("MEGA", "SOURCE_DOWNLOAD", False, False),
        ("GOOGLE_DRIVE", "SOURCE_DOWNLOAD", False, True),
        ("MEGA", "DESTINATION_UPLOAD", False, True),
    ],
)
def test_automatic_omission_requires_verified_file_scope_and_safe_cleanup(
    provider: str,
    provider_stage: str,
    copy_started: bool,
    cleanup_succeeded: bool,
):
    diagnostic = classify_megacmd_failure(1, b"API_EBLOCKED")

    assert not can_auto_omit_mega_failure(
        SourceFileErrorPolicy.SKIP_CONFIRMED_RESTRICTED,
        diagnostic,
        provider=provider,
        provider_stage=provider_stage,
        copy_started=copy_started,
        cleanup_succeeded=cleanup_succeeded,
    )
