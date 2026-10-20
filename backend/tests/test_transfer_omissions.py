import uuid
from datetime import UTC, datetime

import pytest

from app.services import transfer_omissions
from app.services.memory_store import MemoryStore, TransferManifestItemRecord
from app.services.transfer_omissions import resolve_failure_evidence


@pytest.mark.parametrize(
    ("failure_stage", "failure_attempt", "copy_started", "is_dir", "status", "expected"),
    [
        ("SOURCE_DOWNLOAD", 2, False, False, "FAILED", True),
        ("DESTINATION_UPLOAD", 2, False, False, "FAILED", False),
        ("SOURCE_DOWNLOAD", 1, False, False, "FAILED", False),
        ("SOURCE_DOWNLOAD", 2, True, False, "FAILED", False),
        ("SOURCE_DOWNLOAD", 2, False, True, "FAILED", False),
        ("SOURCE_DOWNLOAD", 2, False, False, "COPIED", False),
    ],
)
def test_skip_eligibility_requires_current_individual_source_failure(
    failure_stage, failure_attempt, copy_started, is_dir, status, expected
):
    assert hasattr(transfer_omissions, "can_skip_manifest_item")
    can_skip = transfer_omissions.can_skip_manifest_item
    store = MemoryStore()
    user = store.add_user("skip-owner", "skip@example.test", "hash")
    source = store.create_connection("MEGA", "MEGA", user.id)
    destination = store.create_connection("Seafile", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/x"], "/y")
    transfer.source_provider_type = "MEGA"
    transfer.status = "FAILED"
    transfer.attempt_count = 2
    item = TransferManifestItemRecord(
        id=uuid.uuid4(),
        transfer_id=transfer.id,
        source_path="/x/file.rar",
        destination_path="/y/file.rar",
        is_dir=is_dir,
        status=status,
        copy_started=copy_started,
        error_code="MEGACMD_COMMAND_FAILED",
    )

    assert (
        can_skip(
            transfer,
            item,
            failure_stage=failure_stage,
            failure_attempt=failure_attempt,
        )
        is expected
    )


def test_skip_eligibility_rejects_known_account_quota_and_destination_failures():
    assert hasattr(transfer_omissions, "can_skip_manifest_item")
    can_skip = transfer_omissions.can_skip_manifest_item
    store = MemoryStore()
    user = store.add_user("skip-owner", "skip@example.test", "hash")
    source = store.create_connection("MEGA", "MEGA", user.id)
    destination = store.create_connection("Seafile", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/x"], "/y")
    transfer.source_provider_type = "MEGA"
    transfer.status = "FAILED"
    item = TransferManifestItemRecord(
        id=uuid.uuid4(),
        transfer_id=transfer.id,
        source_path="/x/file.rar",
        destination_path="/y/file.rar",
        is_dir=False,
        status="FAILED",
        error_code="MEGACMD_ACCOUNT_RESTRICTED",
    )

    for error_code in (
        "MEGACMD_ACCOUNT_RESTRICTED",
        "MEGACMD_QUOTA_EXCEEDED",
        "MEGACMD_AUTHENTICATION_FAILED",
        "MEGACMD_CLEANUP_FAILED",
        "DESTINATION_STATE_UNCERTAIN",
    ):
        item.error_code = error_code
        assert not can_skip(
            transfer, item, failure_stage="SOURCE_DOWNLOAD", failure_attempt=1
        )
    item.error_code = "MEGACMD_COMMAND_FAILED"
    transfer.source_provider_type = "GOOGLE_DRIVE"
    assert not can_skip(
        transfer, item, failure_stage="SOURCE_DOWNLOAD", failure_attempt=1
    )


def test_skip_eligibility_rejects_exhausted_transient_source_failure():
    store = MemoryStore()
    user = store.add_user("transient-owner", "transient@example.test", "hash")
    source = store.create_connection("MEGA", "MEGA", user.id)
    destination = store.create_connection("Seafile", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/x"], "/y")
    transfer.source_provider_type = "MEGA"
    transfer.status = "FAILED"
    transfer.attempt_count = 2
    item = TransferManifestItemRecord(
        id=uuid.uuid4(),
        transfer_id=transfer.id,
        source_path="/x/file.rar",
        destination_path="/y/file.rar",
        is_dir=False,
        status="FAILED",
        error_code="MEGACMD_RETRIES_EXHAUSTED",
    )

    assert not transfer_omissions.can_skip_manifest_item(
        transfer,
        item,
        failure_stage="SOURCE_DOWNLOAD",
        failure_attempt=2,
        failure_retryable=True,
    )


@pytest.mark.parametrize(
    (
        "basename_matches",
        "event_stage",
        "created_at",
        "global_attempt",
        "retryable",
        "expected",
    ),
    [
        (1, "SOURCE_DOWNLOAD", datetime.now(UTC), None, False, ("SOURCE_DOWNLOAD", 2, False)),
        (2, "SOURCE_DOWNLOAD", datetime.now(UTC), None, False, (None, None, None)),
        (1, "SOURCE_DOWNLOAD", datetime(2026, 1, 1, tzinfo=UTC), None, False, (None, None, None)),
        (1, "DESTINATION_UPLOAD", datetime.now(UTC), None, False, (None, None, None)),
        (1, "SOURCE_DOWNLOAD", datetime.now(UTC), 1, False, (None, None, None)),
        (1, "SOURCE_DOWNLOAD", datetime.now(UTC), 2, True, ("SOURCE_DOWNLOAD", 2, True)),
    ],
)
def test_historical_diagnostic_must_match_unique_file_current_attempt_and_stage(
    basename_matches, event_stage, created_at, global_attempt, retryable, expected
):
    store = MemoryStore()
    user = store.add_user("history-owner", "history@example.test", "hash")
    source = store.create_connection("MEGA", "MEGA", user.id)
    destination = store.create_connection("Seafile", "SEAFILE", user.id)
    transfer = store.create_transfer(user.id, source.id, destination.id, ["/x"], "/y")
    transfer.status = "FAILED"
    transfer.attempt_count = 2
    transfer.started_at = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
    item = TransferManifestItemRecord(
        id=uuid.uuid4(),
        transfer_id=transfer.id,
        source_path="/x/file.rar",
        destination_path="/y/file.rar",
        is_dir=False,
        status="FAILED",
        error_code="MEGACMD_COMMAND_FAILED",
    )
    details = {
        "file_name": "file.rar",
        "provider": "MEGA",
        "provider_stage": event_stage,
        "destination_write_started": False,
        "retryable": retryable,
    }
    if global_attempt is not None:
        details["transfer_attempt"] = global_attempt
    events = [
        {
            "event_type": "FAILURE_DIAGNOSTIC",
            "technical_details": details,
            "provider_error_code": "MEGACMD_COMMAND_FAILED",
            "created_at": created_at,
        }
    ]

    assert (
        resolve_failure_evidence(
            transfer,
            item,
            events,
            basename_matches=basename_matches,
        )
        == expected
    )
