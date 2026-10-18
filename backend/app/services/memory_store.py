from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from app.security.sanitize import sanitize_data
from app.services.transfer_evaluation import (
    EvaluationClaim,
    EvaluationConflictError,
    EvaluationRecord,
    not_required_evaluation,
    restart_evaluation,
)
from app.services.transfer_omissions import normalize_source_file_error_policy
from app.services.transfer_progress import smb_manifest_progress_percent
from app.settings import Settings

ACTIVE_TRANSFER_STATUSES = frozenset(
    {"SCHEDULED", "QUEUED", "VALIDATING", "RUNNING", "RETRYING", "CANCELLING"}
)
_NOTIFICATION_LEASE_SECONDS = 300
_EVALUATION_LEASE_SECONDS = 60

if TYPE_CHECKING:
    from app.services.source_inventory import InventoryProgress


class ConnectionInUseError(ValueError):
    """Raised when a connection has an active transfer and cannot be archived."""


class ConnectionUnavailableError(ValueError):
    """Raised when a connection is missing, archived, or inactive."""

    def __init__(self, reason: Literal["not_found", "inactive"]) -> None:
        self.reason = reason
        message = "Connection not found" if reason == "not_found" else "Connection is inactive"
        super().__init__(message)


class IdempotencyConflictError(ValueError):
    """Raised when a caller reuses an idempotency key for different input."""


@dataclass
class UserAccount:
    id: uuid.UUID
    username: str
    email: str
    password_hash: str
    role: str
    is_active: bool = True
    must_change_password: bool = False
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None


@dataclass
class ConnectionRecord:
    id: uuid.UUID
    name: str
    provider_type: str
    created_by: uuid.UUID
    adapter_type: str = "RCLONE"
    encrypted_credentials: bytes = b""
    credentials_configured: bool = False
    status: str = "ACTIVE"
    last_tested_at: datetime | None = None
    last_test_status: str | None = None
    is_global: bool = True
    deleted_at: datetime | None = None
    credential_version: int = 1

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "provider_type": self.provider_type,
            "adapter_type": self.adapter_type,
            "is_global": self.is_global,
            "status": self.status,
            "credentials_configured": bool(self.encrypted_credentials),
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "last_test_status": self.last_test_status,
        }


@dataclass
class SMTPSettingsRecord:
    id: uuid.UUID
    host: str
    port: int
    use_tls: bool
    from_address: str
    encrypted_credentials: bytes


@dataclass
class TransferRecord:
    id: uuid.UUID
    public_id: str
    requested_by: uuid.UUID
    source_connection_id: uuid.UUID
    destination_connection_id: uuid.UUID
    source_paths: list[str]
    destination_path: str
    source_item_ids: list[str | None] | None = None
    source_parent_ids: list[str | None] | None = None
    source_credential_version: int = 1
    destination_credential_version: int = 1
    conflict_policy: str = "KEEP_BOTH"
    send_email_notification: bool = False
    status: str = "QUEUED"
    scheduled_at: datetime | None = None
    schedule_timezone: str = "UTC"
    execution_mode: str = "AUTO"
    depends_on_transfer_id: uuid.UUID | None = None
    source_file_error_policy: str = "STOP"
    accepted_parent_omissions_attempt: int | None = None
    source_provider_type: str | None = field(default=None, repr=False, compare=False)
    wait_reason: str | None = None
    dispatch_token: uuid.UUID | None = None
    dispatch_claimed_at: datetime | None = None
    execution_started_at: datetime | None = None
    execution_heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    progress_percent: float = 0.0
    files_total: int = 0
    files_completed: int = 0
    bytes_total: int = 0
    bytes_transferred: int = 0
    current_item: str | None = None
    average_speed_bps: int | None = None
    current_speed_bps: int | None = None
    estimated_remaining_seconds: int | None = None
    attempt_count: int = 1
    error_count: int = 0
    warning_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    idempotent_replay: bool = False
    manifest_summary: dict[str, int] | None = None
    evaluation: EvaluationRecord | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            **(
                {"manifest_summary": self.manifest_summary}
                if self.manifest_summary is not None
                else {}
            ),
            "id": str(self.id),
            "public_id": self.public_id,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "files_total": self.files_total,
            "files_completed": self.files_completed,
            "bytes_total": self.bytes_total,
            "bytes_transferred": self.bytes_transferred,
            "current_item": self.current_item,
            "average_speed_bps": self.average_speed_bps,
            "current_speed_bps": self.current_speed_bps,
            "estimated_remaining_seconds": self.estimated_remaining_seconds,
            "attempt_count": self.attempt_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "source_connection_id": str(self.source_connection_id),
            "destination_connection_id": str(self.destination_connection_id),
            "source_paths": self.source_paths,
            "destination_path": self.destination_path,
            "conflict_policy": self.conflict_policy,
            "send_email_notification": self.send_email_notification,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "schedule_timezone": self.schedule_timezone,
            "execution_mode": self.execution_mode,
            "depends_on_transfer_id": (
                str(self.depends_on_transfer_id) if self.depends_on_transfer_id else None
            ),
            "source_file_error_policy": self.source_file_error_policy,
            "accepted_parent_omissions_attempt": self.accepted_parent_omissions_attempt,
            "wait_reason": self.wait_reason,
            "evaluation": (
                self.evaluation.public_dict() if self.evaluation else not_required_evaluation()
            ),
            "can_edit_schedule": (
                self.status in {"SCHEDULED", "QUEUED"}
                and self.dispatch_token is None
                and self.execution_started_at is None
            ),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class TransferManifestItemRecord:
    id: uuid.UUID
    transfer_id: uuid.UUID
    source_path: str
    destination_path: str
    is_dir: bool
    source_object_id: str | None = None
    requested_destination_path: str | None = None
    rename_reason: str | None = None
    size_bytes: int | None = None
    modified_at: datetime | None = None
    scan_complete: bool = False
    status: str = "PENDING"
    copy_started: bool = False
    destination_existed_before: bool = False
    error_code: str | None = None
    verified_by: str | None = None
    omission_kind: str | None = None
    omitted_at: datetime | None = None
    omitted_by: uuid.UUID | None = None
    last_failure_stage: str | None = None
    last_failure_attempt: int | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_path": self.source_path,
            "source_object_id": self.source_object_id,
            "destination_path": self.destination_path,
            "requested_destination_path": self.requested_destination_path or self.destination_path,
            "rename_reason": self.rename_reason,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "scan_complete": self.scan_complete,
            "status": self.status,
            "error_code": self.error_code,
            "verified_by": self.verified_by,
            "omission_kind": self.omission_kind,
            "omitted_at": self.omitted_at.isoformat() if self.omitted_at else None,
            "last_failure_stage": self.last_failure_stage,
            "last_failure_attempt": self.last_failure_attempt,
        }


def is_programmed_background_transfer(transfer: TransferRecord) -> bool:
    return transfer.status in ACTIVE_TRANSFER_STATUSES and bool(
        transfer.scheduled_at is not None
        or transfer.wait_reason is not None
        or transfer.execution_mode == "EXCLUSIVE"
        or transfer.depends_on_transfer_id is not None
    )


class MemoryStore:
    """In-memory repository for the mock vertical slice and isolated tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.users: dict[uuid.UUID, UserAccount] = {}
        self.connections: dict[uuid.UUID, ConnectionRecord] = {}
        self._connection_credential_fingerprints: dict[uuid.UUID, bytes] = {}
        self.smtp_settings: SMTPSettingsRecord | None = None
        self.transfers: dict[uuid.UUID, TransferRecord] = {}
        self.connection_grants: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.transfer_events: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.notification_outbox: dict[uuid.UUID, dict[str, Any]] = {}
        self.transfer_manifest_items: dict[tuple[uuid.UUID, str], TransferManifestItemRecord] = {}
        self.transfer_idempotency: dict[tuple[uuid.UUID, uuid.UUID], tuple[str, uuid.UUID]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self._evaluation_claim_lock = asyncio.Lock()
        self._transfer_execution_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._transfer_recovery_locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def acheck(self) -> bool:
        return True

    async def aget_smtp_settings(self) -> SMTPSettingsRecord | None:
        return self.smtp_settings

    async def acreate_smtp_settings_if_absent(
        self, settings: SMTPSettingsRecord
    ) -> SMTPSettingsRecord:
        if self.smtp_settings is None:
            self.smtp_settings = settings
        return self.smtp_settings

    async def asave_smtp_settings(self, settings: SMTPSettingsRecord) -> SMTPSettingsRecord:
        self.smtp_settings = settings
        return settings

    def add_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        user = UserAccount(
            id=uuid.uuid4(),
            username=username,
            email=email,
            password_hash=password_hash,
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        self.users[user.id] = user
        return user

    def find_user(self, username_or_email: str) -> UserAccount | None:
        normalized = username_or_email.casefold()
        return next(
            (
                user
                for user in self.users.values()
                if user.username.casefold() == normalized or user.email.casefold() == normalized
            ),
            None,
        )

    def get_user(self, user_id: uuid.UUID) -> UserAccount | None:
        return self.users.get(user_id)

    def create_connection(
        self,
        name: str,
        provider_type: str,
        created_by: uuid.UUID,
        connection_id: uuid.UUID | None = None,
        encrypted_credentials: bytes = b"",
        is_global: bool = True,
        adapter_type: str = "RCLONE",
    ) -> ConnectionRecord:
        record = ConnectionRecord(
            id=connection_id or uuid.uuid4(),
            name=name,
            provider_type=provider_type,
            adapter_type=adapter_type,
            created_by=created_by,
            encrypted_credentials=encrypted_credentials,
            is_global=is_global,
        )
        self.connections[record.id] = record
        self._connection_credential_fingerprints[record.id] = encrypted_credentials
        return record

    def get_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        connection = self.connections.get(connection_id)
        return connection if connection and connection.deleted_at is None else None

    def list_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        connections: Iterable[ConnectionRecord] = (
            connection for connection in self.connections.values() if connection.deleted_at is None
        )
        if user_id is not None:
            connections = (
                connection
                for connection in connections
                if connection.is_global or (user_id, connection.id) in self.connection_grants
            )
        return sorted(connections, key=lambda record: record.name.casefold())

    def next_public_id(self, now: datetime | None = None) -> str:
        year = (now or datetime.now(UTC)).year
        count = sum(
            1
            for transfer in self.transfers.values()
            if transfer.public_id.startswith(f"TRF-{year}-")
        )
        return f"TRF-{year}-{count + 1:06d}"

    def create_transfer(
        self,
        requested_by: uuid.UUID,
        source_connection_id: uuid.UUID,
        destination_connection_id: uuid.UUID,
        source_paths: list[str],
        destination_path: str,
        conflict_policy: str = "KEEP_BOTH",
        send_email_notification: bool = False,
        source_credential_version: int = 1,
        destination_credential_version: int = 1,
        scheduled_at: datetime | None = None,
        schedule_timezone: str = "UTC",
        execution_mode: str = "AUTO",
        depends_on_transfer_id: uuid.UUID | None = None,
        source_item_ids: list[str | None] | None = None,
        source_parent_ids: list[str | None] | None = None,
        source_file_error_policy: str = "STOP",
    ) -> TransferRecord:
        source_file_error_policy = normalize_source_file_error_policy(source_file_error_policy)
        is_scheduled = scheduled_at is not None and scheduled_at > datetime.now(UTC)
        record = TransferRecord(
            id=uuid.uuid4(),
            public_id=self.next_public_id(),
            requested_by=requested_by,
            source_connection_id=source_connection_id,
            destination_connection_id=destination_connection_id,
            source_credential_version=source_credential_version,
            destination_credential_version=destination_credential_version,
            source_paths=source_paths,
            destination_path=destination_path,
            source_item_ids=source_item_ids,
            source_parent_ids=source_parent_ids,
            conflict_policy=conflict_policy,
            send_email_notification=send_email_notification,
            status="SCHEDULED" if is_scheduled else "QUEUED",
            scheduled_at=scheduled_at,
            schedule_timezone=schedule_timezone,
            execution_mode=execution_mode,
            depends_on_transfer_id=depends_on_transfer_id,
            source_file_error_policy=source_file_error_policy,
            wait_reason="WAITING_UNTIL" if is_scheduled else None,
        )
        if is_scheduled:
            source_connection = self.connections.get(source_connection_id)
            destination_connection = self.connections.get(destination_connection_id)
            is_smb_source = bool(
                source_connection is not None and source_connection.provider_type == "SMB"
            )
            record.evaluation = EvaluationRecord(
                id=uuid.uuid4(),
                transfer_id=record.id,
                status="PENDING",
                max_files=(
                    self.settings.smb_transfer_max_files
                    if is_smb_source
                    else self.settings.transfer_max_files
                ),
                source_credential_version=(
                    source_connection.credential_version if source_connection is not None else None
                ),
                destination_credential_version=(
                    destination_connection.credential_version
                    if destination_connection is not None
                    else None
                ),
            )
        self.transfers[record.id] = record
        return record

    def get_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        return self.transfers.get(transfer_id)

    def list_transfers(
        self,
        requested_by: uuid.UUID | None = None,
        pending_only: bool = False,
        background_only: bool = False,
        exclude_background: bool = False,
    ) -> list[TransferRecord]:
        records = [
            record
            for record in self.transfers.values()
            if (requested_by is None or record.requested_by == requested_by)
            and (not pending_only or record.status in {"SCHEDULED", "QUEUED"})
            and (not background_only or is_programmed_background_transfer(record))
            and (not exclude_background or not is_programmed_background_transfer(record))
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    async def aget_user(self, user_id: uuid.UUID) -> UserAccount | None:
        return self.get_user(user_id)

    async def alist_users(self) -> list[UserAccount]:
        return sorted(self.users.values(), key=lambda user: user.username.casefold())

    async def acount_active_admins(self) -> int:
        return sum(1 for user in self.users.values() if user.is_active and user.role == "ADMIN")

    async def acreate_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        if self.find_user(username) or self.find_user(email):
            raise ValueError("Username or email is already in use")
        return self.add_user(username, email, password_hash, role, is_active, must_change_password)

    async def afind_user(self, username_or_email: str) -> UserAccount | None:
        return self.find_user(username_or_email)

    async def arecord_login_failure(
        self, user_id: uuid.UUID, expected_password_hash: str, now: datetime
    ) -> None:
        user = self.users.get(user_id)
        if user is None or user.password_hash != expected_password_hash:
            return
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)

    async def arecord_login_success(
        self, user_id: uuid.UUID, expected_password_hash: str, now: datetime
    ) -> UserAccount | None:
        user = self.users.get(user_id)
        if (
            user is None
            or user.password_hash != expected_password_hash
            or not user.is_active
            or (user.locked_until is not None and user.locked_until > now)
        ):
            return None
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        return user

    async def aupdate_user_password(
        self,
        user_id: uuid.UUID,
        password_hash: str,
        *,
        must_change_password: bool,
        reset_login_failures: bool = False,
        expected_password_hash: str | None = None,
    ) -> UserAccount | None:
        user = self.users.get(user_id)
        if user is None or (
            expected_password_hash is not None and user.password_hash != expected_password_hash
        ):
            return None
        user.password_hash = password_hash
        user.must_change_password = must_change_password
        if reset_login_failures:
            user.failed_login_attempts = 0
            user.locked_until = None
        return user

    async def aupdate_user_profile(
        self, user_id: uuid.UUID, changes: dict[str, str | bool]
    ) -> tuple[UserAccount | None, bool]:
        allowed = {"username", "email", "role", "is_active"}
        if set(changes) - allowed:
            raise ValueError("Unsupported user profile field")
        user = self.users.get(user_id)
        if user is None:
            return None, False
        removes_admin = (
            user.is_active
            and user.role == "ADMIN"
            and (changes.get("is_active", True) is False or changes.get("role") == "USER")
        )
        if removes_admin and await self.acount_active_admins() <= 1:
            return None, True
        username = changes.get("username", user.username)
        email = changes.get("email", user.email)
        if not isinstance(username, str) or not isinstance(email, str):
            raise ValueError("Invalid user profile value")
        existing_username = self.find_user(username)
        existing_email = self.find_user(email)
        if any(
            existing is not None and existing.id != user.id
            for existing in (existing_username, existing_email)
        ):
            raise ValueError("Username or email is already in use")
        for field_name, value in changes.items():
            setattr(user, field_name, value)
        return user, False

    async def aget_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        return self.get_connection(connection_id)

    async def aarchive_connection(
        self,
        connection_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectionRecord | None:
        connection = self.connections.get(connection_id)
        if connection is None or connection.deleted_at is not None:
            return None
        if any(
            transfer.status in ACTIVE_TRANSFER_STATUSES
            and connection_id in {transfer.source_connection_id, transfer.destination_connection_id}
            for transfer in self.transfers.values()
        ):
            raise ConnectionInUseError("Connection has an active transfer")
        connection.deleted_at = datetime.now(UTC)
        connection.encrypted_credentials = b""
        self.connection_grants = {
            grant for grant in self.connection_grants if grant[1] != connection_id
        }
        self.audit_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": str(actor_user_id),
                "action": "CONNECTION_ARCHIVED",
                "resource_type": "connection",
                "resource_id": str(connection.id),
                "result": "SUCCESS",
                "metadata": sanitize_data(
                    {"provider_type": connection.provider_type, "is_global": connection.is_global}
                ),
                "ip_address": ip_address,
                "user_agent": user_agent[:255] if user_agent else None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return connection

    async def alist_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        return self.list_connections(user_id)

    async def ahas_connection_access(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        connection = self.get_connection(connection_id)
        return bool(
            connection
            and (connection.is_global or (user_id, connection_id) in self.connection_grants)
        )

    async def agrant_connection(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, granted_by: uuid.UUID
    ) -> None:
        del granted_by
        if self.get_connection(connection_id) is None:
            raise ConnectionUnavailableError("not_found")
        self.connection_grants.add((user_id, connection_id))

    async def arevoke_connection(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self.connection_grants.discard((user_id, connection_id))

    async def acreate_connection(
        self,
        name: str,
        provider_type: str,
        created_by: uuid.UUID,
        connection_id: uuid.UUID,
        encrypted_credentials: bytes,
        is_global: bool = True,
        adapter_type: str = "RCLONE",
    ) -> ConnectionRecord:
        return self.create_connection(
            name,
            provider_type,
            created_by,
            connection_id,
            encrypted_credentials,
            is_global,
            adapter_type,
        )

    async def asave_connection(self, connection: ConnectionRecord) -> None:
        current = self.connections.get(connection.id)
        if current is None or current.deleted_at is not None:
            raise ConnectionUnavailableError("not_found")
        connection.deleted_at = current.deleted_at
        previous_credentials = self._connection_credential_fingerprints.get(
            connection.id, current.encrypted_credentials
        )
        if connection.encrypted_credentials != previous_credentials:
            connection.credential_version = current.credential_version + 1
        else:
            connection.credential_version = current.credential_version
        self._connection_credential_fingerprints[connection.id] = connection.encrypted_credentials
        self.connections[connection.id] = connection

    async def arequest_transfer_evaluation(
        self, transfer_id: uuid.UUID, now: datetime
    ) -> EvaluationRecord:
        from app.services.transfer_scheduling import (
            dependency_error_code,
            dependency_wait_message,
            dependency_wait_reason,
        )

        now = now.astimezone(UTC)
        async with self._evaluation_claim_lock:
            transfer = self.transfers.get(transfer_id)
            if (
                transfer is None
                or transfer.status not in {"SCHEDULED", "QUEUED"}
                or transfer.dispatch_token is not None
                or transfer.execution_started_at is not None
                or transfer.cancel_requested_at is not None
            ):
                raise EvaluationConflictError(
                    "Solo se puede reevaluar una transferencia pendiente de ejecución."
                )
            evaluation = transfer.evaluation
            if evaluation is None:
                raise EvaluationConflictError(
                    "Esta transferencia no tiene una evaluación anticipada para repetir."
                )
            if evaluation.status in {"PENDING", "EVALUATING", "WAITING_PARENT"}:
                return evaluation

            restart_evaluation(evaluation, now=now)
            parent_wait = None
            if transfer.depends_on_transfer_id is not None:
                parent = self.transfers.get(transfer.depends_on_transfer_id)
                parent_omitted_count = sum(
                    1
                    for (candidate_transfer_id, _), item in self.transfer_manifest_items.items()
                    if parent is not None
                    and candidate_transfer_id == parent.id
                    and not item.is_dir
                    and item.omission_kind is not None
                )
                parent_wait = dependency_wait_reason(
                    parent.status if parent else "FAILED",
                    parent_omitted_count=parent_omitted_count,
                    parent_attempt_count=parent.attempt_count if parent else 1,
                    accepted_parent_omissions_attempt=(
                        transfer.accepted_parent_omissions_attempt
                    ),
                )
            if parent_wait is not None:
                evaluation.status = "WAITING_PARENT"
                evaluation.sequence = 1
                evaluation.safe_error_code = dependency_error_code(parent_wait)
                evaluation.safe_message = dependency_wait_message(parent_wait)
                transfer.wait_reason = parent_wait
            else:
                transfer.wait_reason = "WAITING_EVALUATION"
            return evaluation

    async def aget_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        return self.get_transfer(transfer_id)

    async def alist_transfer_evaluations(
        self, transfer_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, EvaluationRecord]:
        return {
            transfer_id: transfer.evaluation
            for transfer_id in transfer_ids
            if (transfer := self.transfers.get(transfer_id)) is not None
            and transfer.evaluation is not None
        }

    @asynccontextmanager
    async def transfer_execution_lock(self, transfer_id: uuid.UUID) -> AsyncIterator[bool]:
        lock = self._transfer_execution_locks.setdefault(transfer_id, asyncio.Lock())
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    def _evaluation_claim_matches(self, claim: EvaluationClaim) -> bool:
        transfer = self.transfers.get(claim.transfer_id)
        evaluation = transfer.evaluation if transfer is not None else None
        return bool(
            transfer is not None
            and evaluation is not None
            and evaluation.status == "EVALUATING"
            and evaluation.generation == claim.generation
            and evaluation.claim_token == claim.claim_token
            and transfer.status in {"SCHEDULED", "QUEUED"}
            and transfer.dispatch_token is None
            and transfer.execution_started_at is None
            and transfer.cancel_requested_at is None
        )

    async def aclaim_due_evaluations(
        self, now: datetime, *, limit: int = 1
    ) -> list[EvaluationClaim]:
        from app.services.transfer_scheduling import (
            dependency_error_code,
            dependency_wait_message,
            dependency_wait_reason,
        )

        if limit < 1:
            raise ValueError("limit must be positive")
        now = now.astimezone(UTC)
        stale_before = now - timedelta(seconds=_EVALUATION_LEASE_SECONDS)
        claims: list[EvaluationClaim] = []

        def parent_evaluation_ready(transfer: TransferRecord) -> bool:
            if transfer.depends_on_transfer_id is None:
                return False
            parent = self.transfers.get(transfer.depends_on_transfer_id)
            return parent is not None and parent.status in {
                "COMPLETED",
                "COMPLETED_WITH_WARNINGS",
            }

        async with self._evaluation_claim_lock:
            candidates = sorted(
                (
                    transfer
                    for transfer in self.transfers.values()
                    if transfer.evaluation is not None
                    and (
                        (
                            transfer.evaluation.status == "PENDING"
                            and (
                                transfer.evaluation.retry_at is None
                                or transfer.evaluation.retry_at <= now
                            )
                        )
                        or (
                            transfer.evaluation.status == "EVALUATING"
                            and (
                                transfer.evaluation.heartbeat_at is None
                                or transfer.evaluation.heartbeat_at <= stale_before
                            )
                        )
                        or (
                            transfer.evaluation.status == "WAITING_PARENT"
                            and parent_evaluation_ready(transfer)
                        )
                    )
                ),
                key=lambda transfer: (
                    (
                        transfer.evaluation.updated_at
                        if transfer.evaluation is not None
                        and transfer.evaluation.updated_at is not None
                        else transfer.created_at
                    ),
                    transfer.id,
                ),
            )
            for transfer in candidates[: min(limit, 100)]:
                evaluation = transfer.evaluation
                assert evaluation is not None
                if (
                    transfer.status not in {"SCHEDULED", "QUEUED"}
                    or transfer.dispatch_token is not None
                    or transfer.execution_started_at is not None
                    or transfer.cancel_requested_at is not None
                ):
                    evaluation.claim_token = None
                    evaluation.heartbeat_at = None
                    continue

                if transfer.depends_on_transfer_id is not None:
                    parent = self.transfers.get(transfer.depends_on_transfer_id)
                    parent_omitted_count = sum(
                        1
                        for (candidate_transfer_id, _), item in self.transfer_manifest_items.items()
                        if parent is not None
                        and candidate_transfer_id == parent.id
                        and not item.is_dir
                        and item.omission_kind is not None
                    )
                    parent_wait = dependency_wait_reason(
                        parent.status if parent else "FAILED",
                        parent_omitted_count=parent_omitted_count,
                        parent_attempt_count=parent.attempt_count if parent else 1,
                        accepted_parent_omissions_attempt=(
                            transfer.accepted_parent_omissions_attempt
                        ),
                    )
                    if parent_wait is not None:
                        safe_error_code = dependency_error_code(parent_wait)
                        safe_message = dependency_wait_message(parent_wait)
                        if evaluation.status != "WAITING_PARENT":
                            restart_evaluation(evaluation, now=now)
                            evaluation.status = "WAITING_PARENT"
                            evaluation.sequence = 1
                        elif (
                            evaluation.safe_error_code != safe_error_code
                            or evaluation.safe_message != safe_message
                            or transfer.wait_reason != parent_wait
                        ):
                            evaluation.sequence += 1
                        evaluation.safe_error_code = safe_error_code
                        evaluation.safe_message = safe_message
                        evaluation.updated_at = now
                        transfer.wait_reason = parent_wait
                        continue
                    if evaluation.status == "WAITING_PARENT":
                        restart_evaluation(evaluation, now=now)
                        transfer.wait_reason = "WAITING_EVALUATION"
                elif evaluation.status == "WAITING_PARENT":
                    restart_evaluation(evaluation, now=now)
                    transfer.wait_reason = "WAITING_EVALUATION"

                owner = self.users.get(transfer.requested_by)
                if owner is None or not owner.is_active:
                    transfer.wait_reason = "WAITING_USER_ACTIVE"
                    continue
                source = self.connections.get(transfer.source_connection_id)
                destination = self.connections.get(transfer.destination_connection_id)
                if (
                    source is None
                    or destination is None
                    or source.deleted_at is not None
                    or destination.deleted_at is not None
                    or source.status != "ACTIVE"
                    or destination.status != "ACTIVE"
                ):
                    transfer.wait_reason = "WAITING_CONNECTION_ACTIVE"
                    continue
                if (
                    source.credential_version != transfer.source_credential_version
                    or destination.credential_version != transfer.destination_credential_version
                    or (
                        evaluation.source_credential_version is not None
                        and evaluation.source_credential_version != source.credential_version
                    )
                    or (
                        evaluation.destination_credential_version is not None
                        and evaluation.destination_credential_version
                        != destination.credential_version
                    )
                ):
                    evaluation.status = "BLOCKED"
                    evaluation.safe_error_code = "BLOCKED_CONNECTION_CHANGED"
                    evaluation.safe_message = (
                        "Una conexión o sus credenciales cambiaron. "
                        "Solicita una nueva evaluación."
                    )
                    evaluation.completed_at = now
                    evaluation.retry_at = None
                    evaluation.claim_token = None
                    evaluation.heartbeat_at = None
                    evaluation.sequence += 1
                    evaluation.updated_at = now
                    transfer.wait_reason = "BLOCKED_CONNECTION_CHANGED"
                    continue
                if owner.role != "ADMIN" and any(
                    not connection.is_global
                    and (owner.id, connection.id) not in self.connection_grants
                    for connection in (source, destination)
                ):
                    transfer.wait_reason = "WAITING_CONNECTION_ACCESS"
                    continue

                maximum_files = (
                    self.settings.smb_transfer_max_files
                    if source.provider_type == "SMB"
                    else self.settings.transfer_max_files
                )
                if evaluation.max_files != maximum_files:
                    restart_evaluation(evaluation, now=now, max_files=maximum_files)
                    transfer.wait_reason = "WAITING_EVALUATION"
                    continue
                if evaluation.attempt_count >= 3:
                    evaluation.status = "ERROR"
                    evaluation.safe_error_code = "EVALUATION_ATTEMPTS_EXHAUSTED"
                    evaluation.safe_message = (
                        "No se pudo completar la evaluación tras tres intentos."
                    )
                    evaluation.completed_at = now
                    evaluation.retry_at = None
                    evaluation.claim_token = None
                    evaluation.heartbeat_at = None
                    evaluation.sequence += 1
                    evaluation.updated_at = now
                    transfer.wait_reason = "BLOCKED_EVALUATION"
                    continue
                claim_token = uuid.uuid4()
                evaluation.status = "EVALUATING"
                evaluation.claim_token = claim_token
                evaluation.attempt_count += 1
                evaluation.started_at = evaluation.started_at or now
                evaluation.updated_at = now
                evaluation.heartbeat_at = now
                evaluation.retry_at = None
                evaluation.sequence += 1
                transfer.wait_reason = "WAITING_EVALUATION"
                claims.append(
                    EvaluationClaim(
                        transfer_id=transfer.id,
                        generation=evaluation.generation,
                        claim_token=claim_token,
                    )
                )
        return claims

    async def aheartbeat_evaluation(self, claim: EvaluationClaim, now: datetime) -> bool:
        async with self._evaluation_claim_lock:
            if not self._evaluation_claim_matches(claim):
                return False
            transfer = self.transfers[claim.transfer_id]
            assert transfer.evaluation is not None
            transfer.evaluation.heartbeat_at = now.astimezone(UTC)
            return True

    async def asave_evaluation_progress(
        self, claim: EvaluationClaim, progress: InventoryProgress, now: datetime
    ) -> bool:
        async with self._evaluation_claim_lock:
            if not self._evaluation_claim_matches(claim):
                return False
            transfer = self.transfers[claim.transfer_id]
            evaluation = transfer.evaluation
            assert evaluation is not None
            if (
                evaluation.files_found == progress.files_found
                and evaluation.count_complete == progress.count_complete
            ):
                return True
            evaluation.files_found = progress.files_found
            evaluation.count_complete = progress.count_complete
            evaluation.sequence += 1
            evaluation.updated_at = now.astimezone(UTC)
            return True

    async def afinish_evaluation(
        self,
        claim: EvaluationClaim,
        *,
        status: str,
        progress: InventoryProgress,
        error_code: str | None,
        message: str | None,
        now: datetime,
        retry_at: datetime | None = None,
        wait_reason: str | None = None,
    ) -> bool:
        if status not in {"PENDING", "APPROVED", "BLOCKED", "ERROR", "WAITING_PARENT"}:
            raise ValueError("Invalid evaluation completion status")
        async with self._evaluation_claim_lock:
            if not self._evaluation_claim_matches(claim):
                return False
            transfer = self.transfers[claim.transfer_id]
            evaluation = transfer.evaluation
            assert evaluation is not None
            evaluation.status = status
            evaluation.files_found = progress.files_found
            evaluation.count_complete = progress.count_complete
            evaluation.safe_error_code = error_code
            evaluation.safe_message = message
            evaluation.retry_at = retry_at.astimezone(UTC) if retry_at is not None else None
            evaluation.completed_at = (
                now.astimezone(UTC) if status in {"APPROVED", "BLOCKED", "ERROR"} else None
            )
            evaluation.claim_token = None
            evaluation.heartbeat_at = None
            evaluation.sequence += 1
            evaluation.updated_at = now.astimezone(UTC)
            finished_at = now.astimezone(UTC)
            if status == "APPROVED":
                transfer.wait_reason = (
                    "WAITING_UNTIL"
                    if transfer.scheduled_at is not None and transfer.scheduled_at > finished_at
                    else None
                )
            elif status in {"BLOCKED", "ERROR"}:
                transfer.wait_reason = "BLOCKED_EVALUATION"
            elif status == "WAITING_PARENT":
                transfer.wait_reason = wait_reason or "WAITING_PARENT"
            else:
                transfer.wait_reason = "WAITING_EVALUATION"
            return True

    async def ais_transfer_cancel_requested(self, transfer_id: uuid.UUID) -> bool:
        transfer = self.get_transfer(transfer_id)
        return transfer is not None and transfer.cancel_requested_at is not None

    async def alist_transfers(
        self,
        requested_by: uuid.UUID | None = None,
        pending_only: bool = False,
        background_only: bool = False,
        exclude_background: bool = False,
    ) -> list[TransferRecord]:
        return self.list_transfers(
            requested_by,
            pending_only,
            background_only,
            exclude_background,
        )

    async def alist_active_transfer_ids(self) -> set[uuid.UUID]:
        return {
            transfer.id
            for transfer in self.transfers.values()
            if transfer.status in ACTIVE_TRANSFER_STATUSES
        }

    async def alist_transfer_page(
        self,
        requested_by: uuid.UUID | None,
        *,
        offset: int,
        limit: int,
        pending_only: bool = False,
        background_only: bool = False,
        exclude_background: bool = False,
    ) -> tuple[list[TransferRecord], int]:
        records = await self.alist_transfers(
            requested_by,
            pending_only,
            background_only,
            exclude_background,
        )
        return records[offset : offset + limit], len(records)

    async def acreate_transfer(
        self,
        requested_by: uuid.UUID,
        source_connection_id: uuid.UUID,
        destination_connection_id: uuid.UUID,
        source_paths: list[str],
        destination_path: str,
        conflict_policy: str = "KEEP_BOTH",
        send_email_notification: bool = False,
        scheduled_at: datetime | None = None,
        schedule_timezone: str = "UTC",
        execution_mode: str = "AUTO",
        depends_on_transfer_id: uuid.UUID | None = None,
        idempotency_key: uuid.UUID | None = None,
        idempotency_request_hash: str | None = None,
        audit_event: dict[str, Any] | None = None,
        source_item_ids: list[str | None] | None = None,
        source_parent_ids: list[str | None] | None = None,
        source_file_error_policy: str = "STOP",
    ) -> TransferRecord:
        if (idempotency_key is None) != (idempotency_request_hash is None):
            raise ValueError("Idempotency key and request hash must be supplied together")
        if idempotency_key is not None:
            existing = await self.aget_transfer_by_idempotency(
                requested_by, idempotency_key, idempotency_request_hash or ""
            )
            if existing is not None:
                return existing
        for connection_id in {source_connection_id, destination_connection_id}:
            connection = self.get_connection(connection_id)
            if connection is None:
                raise ConnectionUnavailableError("not_found")
            if connection.status != "ACTIVE":
                raise ConnectionUnavailableError("inactive")
        source = self.get_connection(source_connection_id)
        destination = self.get_connection(destination_connection_id)
        record = self.create_transfer(
            requested_by,
            source_connection_id,
            destination_connection_id,
            source_paths,
            destination_path,
            conflict_policy,
            send_email_notification,
            source.credential_version if source else 1,
            destination.credential_version if destination else 1,
            scheduled_at,
            schedule_timezone,
            execution_mode,
            depends_on_transfer_id,
            source_item_ids,
            source_parent_ids,
            source_file_error_policy,
        )
        if idempotency_key is not None and idempotency_request_hash is not None:
            self.transfer_idempotency[(requested_by, idempotency_key)] = (
                idempotency_request_hash,
                record.id,
            )
        if audit_event is not None:
            event_metadata = dict(audit_event.get("metadata") or {})
            event_metadata.setdefault("public_id", record.public_id)
            await self.aadd_audit_event(
                actor_user_id=requested_by,
                action="TRANSFER_CREATED",
                resource_type="transfer",
                resource_id=str(record.id),
                result="SUCCESS",
                metadata=event_metadata,
                ip_address=audit_event.get("ip_address"),
                user_agent=audit_event.get("user_agent"),
            )
        return record

    async def aget_transfer_by_idempotency(
        self,
        requested_by: uuid.UUID,
        idempotency_key: uuid.UUID,
        request_hash: str,
    ) -> TransferRecord | None:
        entry = self.transfer_idempotency.get((requested_by, idempotency_key))
        if entry is None:
            return None
        stored_hash, transfer_id = entry
        if stored_hash != request_hash:
            raise IdempotencyConflictError("Idempotency key was already used for different input")
        record = self.transfers.get(transfer_id)
        return replace(record, idempotent_replay=True) if record is not None else None

    async def asave_transfer(self, transfer: TransferRecord) -> None:
        self.transfers[transfer.id] = transfer
        self._ensure_notification_outbox(transfer)

    def _ensure_notification_outbox(self, transfer: TransferRecord) -> None:
        if not transfer.send_email_notification or transfer.status not in {
            "COMPLETED",
            "COMPLETED_WITH_WARNINGS",
            "FAILED",
            "CANCELLED",
        }:
            return
        if any(
            item["transfer_job_id"] == transfer.id
            and item["transfer_attempt"] == transfer.attempt_count
            for item in self.notification_outbox.values()
        ):
            return
        user = self.users.get(transfer.requested_by)
        source = self.connections.get(transfer.source_connection_id)
        destination = self.connections.get(transfer.destination_connection_id)
        if user is None or source is None or destination is None:
            return
        omitted_items = [
            item
            for (transfer_id, _), item in self.transfer_manifest_items.items()
            if transfer_id == transfer.id and not item.is_dir and item.omission_kind is not None
        ]
        now = datetime.now(UTC)
        notification_id = uuid.uuid4()
        self.notification_outbox[notification_id] = {
            "id": notification_id,
            "transfer_job_id": transfer.id,
            "transfer_public_id": transfer.public_id,
            "transfer_attempt": transfer.attempt_count,
            "status": "PENDING",
            "attempt_count": 0,
            "snapshot": {
                "public_id": transfer.public_id,
                "status": transfer.status,
                "bytes_transferred": transfer.bytes_transferred,
                "omitted": len(omitted_items),
                "omitted_bytes": sum(item.size_bytes or 0 for item in omitted_items),
                "created_at": transfer.created_at.isoformat(),
                "completed_at": (transfer.completed_at or now).isoformat(),
                "recipient": user.email,
                "source_name": source.name,
                "destination_name": destination.name,
            },
            "next_attempt_at": now,
            "claimed_at": None,
            "claim_token": None,
            "last_error_code": None,
            "created_at": now,
        }

    async def adispatch_pending_notifications(
        self, now: datetime, limit: int = 100, lease_seconds: int = _NOTIFICATION_LEASE_SECONDS
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        now = now.astimezone(UTC)
        dispatch_ids = []
        for item in sorted(
            self.notification_outbox.values(), key=lambda entry: entry["created_at"]
        ):
            stale = item["claimed_at"] is not None and item["claimed_at"] < now - timedelta(
                seconds=lease_seconds
            )
            due = item["status"] == "PENDING" and item["next_attempt_at"] <= now
            if not due and not (item["status"] in {"QUEUED", "SENDING"} and stale):
                continue
            if item["attempt_count"] >= 5:
                item["status"] = "FAILED"
                item["last_error_code"] = "ATTEMPTS_EXHAUSTED"
                item["claimed_at"] = None
                item["claim_token"] = None
                continue
            item["status"] = "QUEUED"
            item["claimed_at"] = now
            item["claim_token"] = uuid.uuid4()
            dispatch_ids.append((item["id"], item["claim_token"]))
            if len(dispatch_ids) >= max(1, min(limit, 500)):
                break
        return dispatch_ids

    async def arelease_notification_dispatch(
        self, notification_id: uuid.UUID, now: datetime, dispatch_token: uuid.UUID
    ) -> None:
        item = self.notification_outbox.get(notification_id)
        if item is None or item["status"] != "QUEUED" or item["claim_token"] != dispatch_token:
            return
        item["status"] = "PENDING"
        item["claimed_at"] = None
        item["claim_token"] = None
        item["next_attempt_at"] = now.astimezone(UTC) + timedelta(seconds=15)
        item["last_error_code"] = "QUEUE_UNAVAILABLE"

    async def aclaim_notification_delivery(
        self,
        notification_id: uuid.UUID,
        now: datetime,
        dispatch_token: uuid.UUID,
        lease_seconds: int = _NOTIFICATION_LEASE_SECONDS,
    ) -> tuple[dict[str, Any], uuid.UUID] | None:
        item = self.notification_outbox.get(notification_id)
        if (
            item is None
            or item["status"] != "QUEUED"
            or item["claim_token"] != dispatch_token
            or item["claimed_at"] is None
            or item["claimed_at"] <= now.astimezone(UTC) - timedelta(seconds=lease_seconds)
        ):
            return None
        if item["attempt_count"] >= 5:
            item["status"] = "FAILED"
            item["last_error_code"] = "ATTEMPTS_EXHAUSTED"
            item["claimed_at"] = None
            item["claim_token"] = None
            return None
        item["status"] = "SENDING"
        item["attempt_count"] += 1
        item["claimed_at"] = now.astimezone(UTC)
        item["claim_token"] = uuid.uuid4()
        return dict(item["snapshot"]), item["claim_token"]

    async def amark_notification_sent(
        self, notification_id: uuid.UUID, claim_token: uuid.UUID
    ) -> None:
        item = self.notification_outbox.get(notification_id)
        if item is not None and item["status"] == "SENDING" and item["claim_token"] == claim_token:
            item["status"] = "SENT"
            item["claimed_at"] = None
            item["claim_token"] = None
            item["last_error_code"] = None

    async def afail_notification_delivery(
        self,
        notification_id: uuid.UUID,
        now: datetime,
        claim_token: uuid.UUID,
        error_code: str = "SMTP_DELIVERY_FAILED",
    ) -> None:
        item = self.notification_outbox.get(notification_id)
        if item is None or item["status"] != "SENDING" or item["claim_token"] != claim_token:
            return
        item["claimed_at"] = None
        item["claim_token"] = None
        item["last_error_code"] = (
            error_code
            if error_code in {"SMTP_CONFIGURATION_INVALID", "SMTP_DELIVERY_FAILED"}
            else "SMTP_DELIVERY_FAILED"
        )
        if item["attempt_count"] >= 5:
            item["status"] = "FAILED"
        else:
            item["status"] = "PENDING"
            item["next_attempt_at"] = now.astimezone(UTC) + timedelta(
                seconds=min(3600, 15 * (2 ** (item["attempt_count"] - 1)))
            )

    async def alist_notification_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        items = sorted(
            self.notification_outbox.values(), key=lambda entry: entry["created_at"], reverse=True
        )[: max(1, min(limit, 200))]
        return [
            {
                "id": str(item["id"]),
                "transfer_public_id": item["transfer_public_id"],
                "transfer_attempt": item["transfer_attempt"],
                "status": item["status"],
                "attempt_count": item["attempt_count"],
                "last_error_code": item["last_error_code"],
                "next_attempt_at": item["next_attempt_at"].isoformat(),
                "created_at": item["created_at"].isoformat(),
            }
            for item in items
        ]

    async def aupdate_transfer_schedule(
        self,
        transfer_id: uuid.UUID,
        *,
        scheduled_at: datetime | None,
        schedule_timezone: str,
        execution_mode: str,
        depends_on_transfer_id: uuid.UUID | None,
    ) -> TransferRecord | None:
        from app.services.transfer_scheduling import (
            dependency_error_code,
            dependency_wait_message,
            dependency_wait_reason,
        )

        transfer = self.transfers.get(transfer_id)
        if (
            transfer is None
            or transfer.status not in {"SCHEDULED", "QUEUED"}
            or transfer.dispatch_token is not None
            or transfer.execution_started_at is not None
        ):
            return None
        seen = {transfer_id}
        ancestor_id = depends_on_transfer_id
        while ancestor_id is not None:
            if ancestor_id in seen:
                raise ValueError("La dependencia crearía un ciclo.")
            seen.add(ancestor_id)
            ancestor = self.transfers.get(ancestor_id)
            if ancestor is None:
                raise ValueError("La transferencia padre no existe.")
            ancestor_id = ancestor.depends_on_transfer_id
        dependency_changed = transfer.depends_on_transfer_id != depends_on_transfer_id
        transfer.scheduled_at = scheduled_at
        transfer.schedule_timezone = schedule_timezone
        transfer.execution_mode = execution_mode
        transfer.depends_on_transfer_id = depends_on_transfer_id
        if dependency_changed:
            transfer.accepted_parent_omissions_attempt = None
        due_later = scheduled_at is not None and scheduled_at > datetime.now(UTC)
        transfer.status = "SCHEDULED" if due_later else "QUEUED"
        transfer.wait_reason = "WAITING_UNTIL" if due_later else None
        if transfer.evaluation is None and scheduled_at is not None:
            source = self.connections.get(transfer.source_connection_id)
            destination = self.connections.get(transfer.destination_connection_id)
            transfer.evaluation = EvaluationRecord(
                id=uuid.uuid4(),
                transfer_id=transfer.id,
                status="PENDING",
                max_files=(
                    self.settings.smb_transfer_max_files
                    if source is not None and source.provider_type == "SMB"
                    else self.settings.transfer_max_files
                ),
                source_credential_version=(
                    source.credential_version if source is not None else None
                ),
                destination_credential_version=(
                    destination.credential_version if destination is not None else None
                ),
            )
        if dependency_changed and transfer.evaluation is not None:
            evaluation = transfer.evaluation
            restart_evaluation(evaluation, now=datetime.now(UTC))
            parent_wait = None
            if depends_on_transfer_id is not None:
                parent = self.transfers.get(depends_on_transfer_id)
                parent_omitted_count = sum(
                    1
                    for (candidate_transfer_id, _), item in self.transfer_manifest_items.items()
                    if parent is not None
                    and candidate_transfer_id == parent.id
                    and not item.is_dir
                    and item.omission_kind is not None
                )
                parent_wait = dependency_wait_reason(
                    parent.status if parent else "FAILED",
                    parent_omitted_count=parent_omitted_count,
                    parent_attempt_count=parent.attempt_count if parent else 1,
                    accepted_parent_omissions_attempt=(
                        transfer.accepted_parent_omissions_attempt
                    ),
                )
            if parent_wait is not None:
                evaluation.status = "WAITING_PARENT"
                evaluation.sequence = 1
                evaluation.safe_error_code = dependency_error_code(parent_wait)
                evaluation.safe_message = dependency_wait_message(parent_wait)
                transfer.wait_reason = parent_wait
            else:
                transfer.wait_reason = (
                    "WAITING_UNTIL" if due_later else "WAITING_EVALUATION"
                )
        return transfer

    async def acancel_transfer(
        self, transfer_id: uuid.UUID, now: datetime
    ) -> tuple[TransferRecord | None, bool]:
        transfer = self.transfers.get(transfer_id)
        if transfer is None or transfer.status in {
            "COMPLETED",
            "COMPLETED_WITH_WARNINGS",
            "FAILED",
            "CANCELLED",
        }:
            return None, False
        transfer.cancel_requested_at = now.astimezone(UTC)
        if transfer.status in {"QUEUED", "SCHEDULED"} and transfer.execution_started_at is None:
            transfer.status = "CANCELLED"
            transfer.completed_at = now.astimezone(UTC)
            transfer.wait_reason = None
            transfer.dispatch_token = None
            transfer.dispatch_claimed_at = None
            transfer.execution_heartbeat_at = None
            self._ensure_notification_outbox(transfer)
            return transfer, False
        transfer.status = "CANCELLING"
        return transfer, True

    async def aresume_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        transfer = self.transfers.get(transfer_id)
        if transfer is None or transfer.status not in {"FAILED", "CANCELLED"}:
            return None
        transfer.status = "QUEUED"
        transfer.attempt_count += 1
        transfer.completed_at = None
        transfer.current_item = None
        transfer.current_speed_bps = None
        transfer.estimated_remaining_seconds = None
        transfer.wait_reason = None
        transfer.dispatch_token = None
        transfer.dispatch_claimed_at = None
        transfer.execution_started_at = None
        transfer.execution_heartbeat_at = None
        transfer.cancel_requested_at = None
        return transfer

    async def aaccept_parent_omissions(
        self,
        child_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        expected_parent_attempt: int,
        request_id: uuid.UUID,
    ) -> TransferRecord:
        from app.services.transfer_evaluation import restart_evaluation
        from app.services.transfer_omissions import (
            TransferRecoveryConflictError,
            TransferRecoveryNotFoundError,
            parent_omissions_request_hash,
        )

        lock = self._transfer_recovery_locks.setdefault(child_id, asyncio.Lock())
        async with lock:
            child = self.transfers.get(child_id)
            if child is None:
                raise TransferRecoveryNotFoundError("Transfer not found")
            request_hash = parent_omissions_request_hash(
                child_id=child_id,
                expected_parent_attempt=expected_parent_attempt,
            )
            actor = self.users.get(actor_id)
            if actor is None or not actor.is_active:
                raise TransferRecoveryNotFoundError("Transfer not found")
            if actor.role != "ADMIN" and child.requested_by != actor_id:
                raise TransferRecoveryNotFoundError("Transfer not found")
            parent = (
                self.transfers.get(child.depends_on_transfer_id)
                if child.depends_on_transfer_id
                else None
            )
            if parent is None or (actor.role != "ADMIN" and parent.requested_by != actor_id):
                raise TransferRecoveryNotFoundError("Parent transfer not found")
            for event in self.audit_events:
                if (
                    event.get("resource_type") == "transfer"
                    and event.get("resource_id") == str(child_id)
                    and event.get("action") == "TRANSFER_PARENT_OMISSIONS_ACCEPTED"
                    and event.get("metadata", {}).get("request_id") == str(request_id)
                ):
                    if event.get("metadata", {}).get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different acceptance"
                        )
                    child.idempotent_replay = True
                    return child

            if child.status not in {"SCHEDULED", "QUEUED"}:
                raise TransferRecoveryConflictError("Child transfer is not pending")
            if (
                child.dispatch_token is not None
                or child.dispatch_claimed_at is not None
                or child.execution_started_at is not None
                or child.execution_heartbeat_at is not None
                or child.cancel_requested_at is not None
                or (
                    execution_lock := self._transfer_execution_locks.get(child_id)
                )
                is not None
                and execution_lock.locked()
            ):
                raise TransferRecoveryConflictError("Child transfer execution is still active")
            if expected_parent_attempt != parent.attempt_count:
                raise TransferRecoveryConflictError("Parent transfer attempt changed")
            if parent.status not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}:
                raise TransferRecoveryConflictError(
                    "Only a successful terminal parent can be accepted"
                )
            summary = (await self.alist_transfer_manifest_summaries([parent.id]))[parent.id]
            if summary.get("omitted", 0) < 1:
                raise TransferRecoveryConflictError("Parent has no explicit omissions")

            previous_child = replace(child)
            event_count = len(self.transfer_events.get(child_id, []))
            audit_count = len(self.audit_events)
            now = datetime.now(UTC)
            try:
                child.accepted_parent_omissions_attempt = expected_parent_attempt
                if child.evaluation is not None:
                    if child.evaluation.status == "WAITING_PARENT":
                        restart_evaluation(child.evaluation, now=now)
                    child.wait_reason = "WAITING_EVALUATION"
                else:
                    child.wait_reason = (
                        "WAITING_UNTIL"
                        if child.scheduled_at is not None and child.scheduled_at > now
                        else None
                    )
                metadata = {
                    "child_public_id": child.public_id,
                    "parent_public_id": parent.public_id,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "accepted_parent_omissions_attempt": expected_parent_attempt,
                    "omitted_count": summary["omitted"],
                }
                await self.aadd_audit_event(
                    actor_user_id=actor_id,
                    action="TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                    resource_type="transfer",
                    resource_id=str(child_id),
                    result="SUCCESS",
                    metadata=metadata,
                )
                await self.aadd_transfer_event(
                    child_id,
                    level="WARNING",
                    event_type="TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                    message=(
                        "Se aceptaron las omisiones del intento vigente de la transferencia padre."
                    ),
                    technical_details={
                        "parent_transfer_id": str(parent.id),
                        "parent_attempt": expected_parent_attempt,
                        "omitted_count": summary["omitted"],
                    },
                )
            except Exception:
                self.transfers[child_id] = previous_child
                del self.transfer_events[child_id][event_count:]
                del self.audit_events[audit_count:]
                raise
            return child

    async def aretry_omitted_transfer_items(
        self,
        transfer_id: uuid.UUID,
        *,
        item_ids: list[uuid.UUID],
        actor_id: uuid.UUID,
        expected_attempt: int,
        request_id: uuid.UUID,
    ) -> TransferRecord:
        from app.services.transfer_omissions import (
            TransferRecoveryConflictError,
            TransferRecoveryNotFoundError,
            retry_omissions_request_hash,
        )

        if not 1 <= len(item_ids) <= 100 or len(item_ids) != len(set(item_ids)):
            raise TransferRecoveryConflictError("Select between 1 and 100 unique omitted files")
        lock = self._transfer_recovery_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = self.transfers.get(transfer_id)
            if transfer is None:
                raise TransferRecoveryNotFoundError("Transfer not found")
            actor = self.users.get(actor_id)
            if actor is None or not actor.is_active:
                raise TransferRecoveryNotFoundError("Transfer not found")
            if actor.role != "ADMIN" and transfer.requested_by != actor_id:
                raise TransferRecoveryNotFoundError("Transfer not found")

            request_hash = retry_omissions_request_hash(
                transfer_id=transfer_id,
                expected_attempt=expected_attempt,
                item_ids=item_ids,
            )
            for event in self.audit_events:
                if (
                    event.get("resource_type") == "transfer"
                    and event.get("resource_id") == str(transfer_id)
                    and event.get("action") == "TRANSFER_OMITTED_FILES_RETRIED"
                    and event.get("metadata", {}).get("request_id") == str(request_id)
                ):
                    if event.get("metadata", {}).get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different retry"
                        )
                    transfer.idempotent_replay = True
                    return transfer

            if expected_attempt != transfer.attempt_count:
                raise TransferRecoveryConflictError("Transfer attempt changed")
            if transfer.status not in {
                "FAILED",
                "CANCELLED",
                "COMPLETED_WITH_WARNINGS",
            }:
                raise TransferRecoveryConflictError("Transfer is not in a retryable terminal state")
            if (
                transfer.dispatch_token is not None
                or transfer.dispatch_claimed_at is not None
                or transfer.execution_started_at is not None
                or transfer.execution_heartbeat_at is not None
                or (
                    execution_lock := self._transfer_execution_locks.get(transfer_id)
                )
                is not None
                and execution_lock.locked()
            ):
                raise TransferRecoveryConflictError("Transfer execution is still active")

            source = self.connections.get(transfer.source_connection_id)
            destination = self.connections.get(transfer.destination_connection_id)
            if source is None or destination is None or source.deleted_at or destination.deleted_at:
                raise TransferRecoveryConflictError("A transfer connection is unavailable")
            if source.status != "ACTIVE" or destination.status != "ACTIVE":
                raise TransferRecoveryConflictError("A transfer connection is inactive")
            if (
                source.credential_version != transfer.source_credential_version
                or destination.credential_version != transfer.destination_credential_version
            ):
                raise TransferRecoveryConflictError("Transfer credentials changed")
            if actor.role != "ADMIN":
                if not source.is_global and (actor_id, source.id) not in self.connection_grants:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if (
                    not destination.is_global
                    and (actor_id, destination.id) not in self.connection_grants
                ):
                    raise TransferRecoveryNotFoundError("Transfer not found")

            items = [self._manifest_item(transfer_id, item_id) for item_id in item_ids]
            if any(item is None for item in items):
                raise TransferRecoveryNotFoundError("Manifest item not found")
            selected = [item for item in items if item is not None]
            if any(
                item.is_dir
                or item.status != "SKIPPED"
                or item.omission_kind is None
                or item.omitted_at is None
                or item.copy_started
                for item in selected
            ):
                raise TransferRecoveryConflictError(
                    "Every selected file must be an explicit omission"
                )

            keys = [
                key
                for key, candidate in self.transfer_manifest_items.items()
                if key[0] == transfer_id and candidate.id in set(item_ids)
            ]
            previous_transfer = replace(transfer)
            previous_items = {
                key: replace(self.transfer_manifest_items[key]) for key in keys
            }
            event_count = len(self.transfer_events.get(transfer_id, []))
            audit_count = len(self.audit_events)
            old_attempt = transfer.attempt_count
            prior_omissions = [
                {
                    "manifest_item_id": str(item.id),
                    "omission_kind": item.omission_kind,
                    "omitted_at": item.omitted_at.isoformat() if item.omitted_at else None,
                }
                for item in selected
            ]
            try:
                for item in selected:
                    item.status = "PENDING"
                    item.error_code = None
                    item.verified_by = None
                    item.omission_kind = None
                    item.omitted_at = None
                    item.omitted_by = None
                    item.last_failure_stage = None
                    item.last_failure_attempt = None
                transfer.status = "QUEUED"
                transfer.attempt_count += 1
                transfer.files_completed = max(0, transfer.files_completed - len(selected))
                transfer.warning_count = max(0, transfer.warning_count - len(selected))
                transfer.completed_at = None
                transfer.started_at = None
                transfer.current_item = None
                transfer.current_speed_bps = None
                transfer.estimated_remaining_seconds = None
                transfer.wait_reason = None
                transfer.dispatch_token = None
                transfer.dispatch_claimed_at = None
                transfer.execution_started_at = None
                transfer.execution_heartbeat_at = None
                transfer.cancel_requested_at = None
                transfer.source_file_error_policy = "STOP"
                counts = await self.aget_transfer_manifest_counts(transfer_id)
                transfer.progress_percent = smb_manifest_progress_percent(
                    bytes_transferred=transfer.bytes_transferred,
                    bytes_total=transfer.bytes_total,
                    files_total=transfer.files_total,
                    counts=counts,
                )
                metadata = {
                    "public_id": transfer.public_id,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "previous_attempt": old_attempt,
                    "attempt_count": transfer.attempt_count,
                    "retried_count": len(selected),
                    "items": prior_omissions,
                }
                await self.aadd_audit_event(
                    actor_user_id=actor_id,
                    action="TRANSFER_OMITTED_FILES_RETRIED",
                    resource_type="transfer",
                    resource_id=str(transfer_id),
                    result="SUCCESS",
                    metadata=metadata,
                )
                await self.aadd_transfer_event(
                    transfer_id,
                    level="WARNING",
                    event_type="TRANSFER_OMITTED_FILES_RETRIED",
                    message="Se reintentan los archivos omitidos seleccionados.",
                    technical_details={
                        "previous_attempt": old_attempt,
                        "attempt_count": transfer.attempt_count,
                        "retried_count": len(selected),
                        "manifest_item_ids": [str(item.id) for item in selected],
                    },
                    correlation_id=request_id,
                )
                transfer.manifest_summary = (
                    await self.alist_transfer_manifest_summaries([transfer_id])
                )[transfer_id]
            except Exception:
                self.transfers[transfer_id] = previous_transfer
                for key, item in previous_items.items():
                    self.transfer_manifest_items[key] = item
                del self.transfer_events[transfer_id][event_count:]
                del self.audit_events[audit_count:]
                raise
            transfer.idempotent_replay = False
            return transfer

    async def askip_transfer_item_and_resume(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        expected_attempt: int,
        request_id: uuid.UUID,
        source_file_error_policy: str | None = None,
    ) -> TransferRecord:
        from app.services.transfer_omissions import (
            OmissionKind,
            TransferRecoveryConflictError,
            TransferRecoveryNotFoundError,
            can_skip_manifest_item,
            normalize_source_file_error_policy,
            recovery_request_hash,
            resolve_failure_evidence,
            skip_unavailable_reason,
        )

        lock = self._transfer_recovery_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = self.transfers.get(transfer_id)
            if transfer is None:
                raise TransferRecoveryNotFoundError("Transfer not found")
            request_hash = recovery_request_hash(
                item_id=item_id,
                expected_attempt=expected_attempt,
                source_file_error_policy=source_file_error_policy,
            )
            for event in self.audit_events:
                if (
                    event.get("resource_type") == "transfer"
                    and event.get("resource_id") == str(transfer_id)
                    and event.get("action") == "TRANSFER_FILE_OMITTED"
                    and event.get("metadata", {}).get("request_id") == str(request_id)
                ):
                    if event.get("metadata", {}).get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different recovery action"
                        )
                    transfer.idempotent_replay = True
                    return transfer

            actor = self.users.get(actor_id)
            if actor is None or not actor.is_active:
                raise TransferRecoveryNotFoundError("Transfer not found")
            if actor.role != "ADMIN" and transfer.requested_by != actor_id:
                raise TransferRecoveryNotFoundError("Transfer not found")
            if expected_attempt != transfer.attempt_count:
                raise TransferRecoveryConflictError("Transfer attempt changed")
            if transfer.status != "FAILED":
                raise TransferRecoveryConflictError("Transfer is not failed")
            if (
                transfer.dispatch_token is not None
                or transfer.dispatch_claimed_at is not None
                or transfer.execution_started_at is not None
                or transfer.execution_heartbeat_at is not None
                or (
                    execution_lock := self._transfer_execution_locks.get(transfer_id)
                )
                is not None
                and execution_lock.locked()
            ):
                raise TransferRecoveryConflictError("Transfer execution is still active")

            source = self.connections.get(transfer.source_connection_id)
            destination = self.connections.get(transfer.destination_connection_id)
            if source is None or destination is None or source.deleted_at or destination.deleted_at:
                raise TransferRecoveryConflictError("A transfer connection is unavailable")
            if source.status != "ACTIVE" or destination.status != "ACTIVE":
                raise TransferRecoveryConflictError("A transfer connection is inactive")
            if (
                source.credential_version != transfer.source_credential_version
                or destination.credential_version != transfer.destination_credential_version
            ):
                raise TransferRecoveryConflictError("Transfer credentials changed")
            if actor.role != "ADMIN":
                if not source.is_global and (actor_id, source.id) not in self.connection_grants:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if (
                    not destination.is_global
                    and (actor_id, destination.id) not in self.connection_grants
                ):
                    raise TransferRecoveryNotFoundError("Transfer not found")

            item = self._manifest_item(transfer_id, item_id)
            if item is None:
                raise TransferRecoveryNotFoundError("Manifest item not found")
            if source_file_error_policy is not None:
                policy = normalize_source_file_error_policy(source_file_error_policy)
                if policy != "STOP":
                    raise TransferRecoveryConflictError(
                        "Automatic source omission is not enabled"
                    )

            all_items = [
                candidate
                for (candidate_transfer_id, _), candidate in self.transfer_manifest_items.items()
                if candidate_transfer_id == transfer_id
            ]
            events = self.transfer_events.get(transfer_id, [])
            basename = PurePosixPath(item.source_path).name
            basename_matches = sum(
                1
                for candidate in all_items
                if not candidate.is_dir and PurePosixPath(candidate.source_path).name == basename
            )
            transfer.source_provider_type = source.provider_type
            failure_stage, failure_attempt = resolve_failure_evidence(
                transfer,
                item,
                events,
                basename_matches=basename_matches,
            )
            if not can_skip_manifest_item(
                transfer,
                item,
                failure_stage=failure_stage,
                failure_attempt=failure_attempt,
            ):
                raise TransferRecoveryConflictError(
                    skip_unavailable_reason(
                        transfer,
                        item,
                        failure_stage=failure_stage,
                        failure_attempt=failure_attempt,
                    )
                    or "Transfer item cannot be omitted"
                )

            key = next(
                key
                for key, candidate in self.transfer_manifest_items.items()
                if key[0] == transfer_id and candidate.id == item_id
            )
            previous_transfer = replace(transfer)
            previous_item = replace(item)
            event_count = len(events)
            audit_count = len(self.audit_events)
            old_attempt = transfer.attempt_count
            now = datetime.now(UTC)
            try:
                item.status = "SKIPPED"
                item.omission_kind = OmissionKind.USER_ACCEPTED_SOURCE_FAILURE.value
                item.omitted_at = now
                item.omitted_by = actor_id
                item.last_failure_stage = failure_stage
                item.last_failure_attempt = failure_attempt
                transfer.error_count = max(0, transfer.error_count - 1)
                transfer.status = "QUEUED"
                transfer.attempt_count += 1
                transfer.completed_at = None
                transfer.started_at = None
                transfer.current_item = None
                transfer.current_speed_bps = None
                transfer.estimated_remaining_seconds = None
                transfer.wait_reason = None
                transfer.dispatch_token = None
                transfer.dispatch_claimed_at = None
                transfer.execution_started_at = None
                transfer.execution_heartbeat_at = None
                transfer.cancel_requested_at = None
                if source_file_error_policy is not None:
                    transfer.source_file_error_policy = normalize_source_file_error_policy(
                        source_file_error_policy
                    )
                metadata = {
                    "public_id": transfer.public_id,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "manifest_item_id": str(item.id),
                    "omission_kind": item.omission_kind,
                    "failure_attempt": failure_attempt,
                    "attempt_count": old_attempt,
                }
                await self.aadd_audit_event(
                    actor_user_id=actor_id,
                    action="TRANSFER_FILE_OMITTED",
                    resource_type="transfer",
                    resource_id=str(transfer_id),
                    result="SUCCESS",
                    metadata=metadata,
                )
                await self.aadd_audit_event(
                    actor_user_id=actor_id,
                    action="TRANSFER_RESUMED",
                    resource_type="transfer",
                    resource_id=str(transfer_id),
                    result="SUCCESS",
                    metadata={
                        "public_id": transfer.public_id,
                        "request_id": str(request_id),
                        "request_hash": request_hash,
                        "attempt_count": transfer.attempt_count,
                    },
                )
                await self.aadd_transfer_event(
                    transfer_id,
                    level="WARNING",
                    event_type="TRANSFER_FILE_OMITTED",
                    message="Se omitió un archivo del origen por decisión del usuario.",
                    technical_details={
                        "manifest_item_id": str(item.id),
                        "omission_kind": item.omission_kind,
                        "failure_attempt": failure_attempt,
                        "attempt_count": old_attempt,
                    },
                    provider_error_code=item.error_code,
                    item_path=item.source_path,
                )
                await self.aadd_transfer_event(
                    transfer_id,
                    level="INFO",
                    event_type="TRANSFER_RESUMED",
                    message="La transferencia continuará con los archivos restantes.",
                    technical_details={"attempt_count": transfer.attempt_count},
                )
            except Exception:
                self.transfers[transfer_id] = previous_transfer
                self.transfer_manifest_items[key] = previous_item
                del self.transfer_events[transfer_id][event_count:]
                del self.audit_events[audit_count:]
                raise
            summaries = await self.alist_transfer_manifest_summaries([transfer_id])
            transfer.manifest_summary = summaries[transfer_id]
            return transfer

    async def adispatch_due_transfers(
        self,
        now: datetime,
        *,
        max_concurrent: int,
        max_nextcloud_concurrent: int = 1,
    ) -> Any:
        from app.services.transfer_scheduling import (
            DispatchBatch,
            TransferReservation,
            admission_wait_reason,
            dependency_error_code,
            dependency_wait_message,
            dependency_wait_reason,
            evaluation_wait_reason,
            transfer_requires_staging,
        )

        if max_concurrent < 1 or max_nextcloud_concurrent < 1:
            raise ValueError("Transfer capacity limits must be positive")
        now = now.astimezone(UTC)
        changed_ids: set[uuid.UUID] = set()
        admitted: list[TransferRecord] = []
        active_statuses = {"VALIDATING", "RUNNING", "RETRYING", "CANCELLING"}
        running = [
            transfer
            for transfer in self.transfers.values()
            if transfer.status in active_statuses
            or (transfer.dispatch_token is not None and transfer.status in ACTIVE_TRANSFER_STATUSES)
        ]

        def uses_staging(transfer: TransferRecord) -> bool:
            source = self.connections.get(transfer.source_connection_id)
            destination = self.connections.get(transfer.destination_connection_id)
            if source is None or destination is None:
                return False
            return transfer_requires_staging(
                source.provider_type,
                destination.provider_type,
                source.adapter_type,
                destination.adapter_type,
                transfer.conflict_policy,
            )

        def uses_nextcloud(transfer: TransferRecord) -> bool:
            return any(
                (connection := self.connections.get(connection_id)) is not None
                and connection.provider_type == "NEXTCLOUD"
                for connection_id in (
                    transfer.source_connection_id,
                    transfer.destination_connection_id,
                )
            )

        active = [
            TransferReservation(
                transfer_id=transfer.id,
                source_connection_id=transfer.source_connection_id,
                destination_connection_id=transfer.destination_connection_id,
                execution_mode=transfer.execution_mode,
                uses_staging=uses_staging(transfer),
                uses_nextcloud=uses_nextcloud(transfer),
            )
            for transfer in running
        ]
        candidates = sorted(
            (
                transfer
                for transfer in self.transfers.values()
                if transfer.status in {"SCHEDULED", "QUEUED"}
                and transfer.dispatch_token is None
            ),
            key=lambda transfer: (
                transfer.scheduled_at or transfer.created_at,
                transfer.created_at,
                transfer.id,
            ),
        )
        exclusive_barrier = False
        for transfer in candidates:
            scheduled_at = transfer.scheduled_at
            if scheduled_at is not None and scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            if scheduled_at is not None and scheduled_at > now:
                if transfer.wait_reason != "WAITING_UNTIL":
                    transfer.wait_reason = "WAITING_UNTIL"
                    changed_ids.add(transfer.id)
                if transfer.status != "SCHEDULED":
                    transfer.status = "SCHEDULED"
                    changed_ids.add(transfer.id)
                continue
            if transfer.status == "SCHEDULED":
                transfer.status = "QUEUED"
                changed_ids.add(transfer.id)

            if transfer.depends_on_transfer_id is not None:
                parent = self.transfers.get(transfer.depends_on_transfer_id)
                parent_omitted_count = sum(
                    1
                    for (candidate_transfer_id, _), item in self.transfer_manifest_items.items()
                    if parent is not None
                    and candidate_transfer_id == parent.id
                    and not item.is_dir
                    and item.omission_kind is not None
                )
                parent_wait = dependency_wait_reason(
                    parent.status if parent else "FAILED",
                    parent_omitted_count=parent_omitted_count,
                    parent_attempt_count=parent.attempt_count if parent else 1,
                    accepted_parent_omissions_attempt=(
                        transfer.accepted_parent_omissions_attempt
                    ),
                )
                if parent_wait is not None:
                    wait_reason_changed = transfer.wait_reason != parent_wait
                    if wait_reason_changed:
                        transfer.wait_reason = parent_wait
                        changed_ids.add(transfer.id)
                    if (
                        transfer.evaluation is not None
                        and transfer.evaluation.status != "WAITING_PARENT"
                    ):
                        evaluation = transfer.evaluation
                        if evaluation.status in {"EVALUATING", "APPROVED", "BLOCKED", "ERROR"}:
                            restart_evaluation(evaluation, now=now)
                        else:
                            evaluation.sequence += 1
                            evaluation.updated_at = now
                        evaluation.status = "WAITING_PARENT"
                        evaluation.safe_error_code = dependency_error_code(parent_wait)
                        evaluation.safe_message = dependency_wait_message(parent_wait)
                    elif transfer.evaluation is not None and (
                        transfer.evaluation.safe_error_code != dependency_error_code(parent_wait)
                        or transfer.evaluation.safe_message
                        != dependency_wait_message(parent_wait)
                        or wait_reason_changed
                    ):
                        transfer.evaluation.safe_error_code = dependency_error_code(parent_wait)
                        transfer.evaluation.safe_message = dependency_wait_message(parent_wait)
                        transfer.evaluation.updated_at = now
                        transfer.evaluation.sequence += 1
                    continue
                if (
                    transfer.evaluation is not None
                    and transfer.evaluation.status == "WAITING_PARENT"
                ):
                    transfer.evaluation.status = "PENDING"
                    transfer.evaluation.safe_message = None
                    transfer.evaluation.updated_at = now
                    transfer.evaluation.sequence += 1

            owner = self.users.get(transfer.requested_by)
            if owner is None or not owner.is_active:
                wait_reason = "WAITING_USER_ACTIVE"
            else:
                source = self.connections.get(transfer.source_connection_id)
                destination = self.connections.get(transfer.destination_connection_id)
                if (
                    source is None
                    or destination is None
                    or source.deleted_at is not None
                    or destination.deleted_at is not None
                    or source.status != "ACTIVE"
                    or destination.status != "ACTIVE"
                ):
                    wait_reason = "WAITING_CONNECTION_ACTIVE"
                elif (
                    source.credential_version != transfer.source_credential_version
                    or destination.credential_version != transfer.destination_credential_version
                ):
                    wait_reason = "BLOCKED_CONNECTION_CHANGED"
                elif owner.role != "ADMIN" and any(
                    not connection.is_global
                    and (owner.id, connection.id) not in self.connection_grants
                    for connection in (source, destination)
                ):
                    wait_reason = "WAITING_CONNECTION_ACCESS"
                else:
                    wait_reason = None
            if wait_reason is not None:
                if transfer.wait_reason != wait_reason:
                    transfer.wait_reason = wait_reason
                    changed_ids.add(transfer.id)
                continue

            assert source is not None and destination is not None
            current_evaluation = transfer.evaluation
            if current_evaluation is not None:
                maximum_files = (
                    self.settings.smb_transfer_max_files
                    if source.provider_type == "SMB"
                    else self.settings.transfer_max_files
                )
                if current_evaluation.max_files != maximum_files:
                    restart_evaluation(current_evaluation, now=now, max_files=maximum_files)
                    transfer.wait_reason = "WAITING_EVALUATION"
                    changed_ids.add(transfer.id)
                    continue
                evaluation_wait = evaluation_wait_reason(current_evaluation)
                if evaluation_wait is not None:
                    if transfer.wait_reason != evaluation_wait:
                        transfer.wait_reason = evaluation_wait
                        changed_ids.add(transfer.id)
                    continue

            if exclusive_barrier:
                wait_reason = "WAITING_EXCLUSIVE"
            else:
                wait_reason = admission_wait_reason(
                    source_connection_id=transfer.source_connection_id,
                    destination_connection_id=transfer.destination_connection_id,
                    execution_mode=transfer.execution_mode,
                    uses_staging=uses_staging(transfer),
                    uses_nextcloud=uses_nextcloud(transfer),
                    active=active,
                    max_concurrent=max_concurrent,
                    max_nextcloud_concurrent=max_nextcloud_concurrent,
                )
            if wait_reason is not None:
                if transfer.wait_reason != wait_reason:
                    transfer.wait_reason = wait_reason
                    changed_ids.add(transfer.id)
                if transfer.execution_mode == "EXCLUSIVE":
                    exclusive_barrier = True
                continue

            transfer.dispatch_token = uuid.uuid4()
            transfer.dispatch_claimed_at = now
            transfer.execution_started_at = None
            transfer.execution_heartbeat_at = None
            transfer.wait_reason = None
            admitted.append(transfer)
            changed_ids.add(transfer.id)
            active.append(
                TransferReservation(
                    transfer_id=transfer.id,
                    source_connection_id=transfer.source_connection_id,
                    destination_connection_id=transfer.destination_connection_id,
                    execution_mode=transfer.execution_mode,
                    uses_staging=uses_staging(transfer),
                    uses_nextcloud=uses_nextcloud(transfer),
                )
            )
            if transfer.execution_mode == "EXCLUSIVE":
                exclusive_barrier = True

        changed = [
            self.transfers[transfer_id]
            for transfer_id in changed_ids
            if transfer_id in self.transfers
        ]
        return DispatchBatch(admitted=admitted, changed=changed)

    async def aadd_transfer_manifest_items(
        self, transfer_id: uuid.UUID, entries: list[dict[str, Any]]
    ) -> int:
        if transfer_id not in self.transfers:
            raise ValueError("Transfer not found")
        inserted = 0
        for entry in entries:
            source_path = str(entry["source_path"])
            source_object_id = entry.get("source_object_id")
            identity = (
                f"{transfer_id}:object:{source_object_id}"
                if isinstance(source_object_id, str) and source_object_id
                else f"{transfer_id}:path:{source_path}"
            )
            digest = hashlib.sha256(identity.encode()).hexdigest()
            key = (transfer_id, digest)
            if key in self.transfer_manifest_items:
                continue
            is_dir = bool(entry.get("is_dir", False))
            self.transfer_manifest_items[key] = TransferManifestItemRecord(
                id=uuid.uuid4(),
                transfer_id=transfer_id,
                source_path=source_path,
                destination_path=str(entry["destination_path"]),
                is_dir=is_dir,
                source_object_id=(source_object_id if isinstance(source_object_id, str) else None),
                requested_destination_path=str(
                    entry.get("requested_destination_path", entry["destination_path"])
                ),
                rename_reason=(str(entry["rename_reason"]) if entry.get("rename_reason") else None),
                size_bytes=entry.get("size_bytes"),
                modified_at=entry.get("modified_at"),
                status="DIRECTORY" if is_dir else "PENDING",
            )
            inserted += 1
        files = [
            item
            for (item_transfer_id, _), item in self.transfer_manifest_items.items()
            if item_transfer_id == transfer_id and not item.is_dir
        ]
        transfer = self.transfers[transfer_id]
        transfer.files_total = len(files)
        transfer.bytes_total = sum(item.size_bytes or 0 for item in files)
        return inserted

    async def alist_pending_scan_directories(
        self, transfer_id: uuid.UUID, limit: int = 200
    ) -> list[TransferManifestItemRecord]:
        rows = [
            item
            for (item_transfer_id, _), item in self.transfer_manifest_items.items()
            if item_transfer_id == transfer_id and item.is_dir and not item.scan_complete
        ]
        return sorted(rows, key=lambda item: item.id.int)[:limit]

    async def amark_transfer_directory_scanned(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID
    ) -> None:
        item = self._manifest_item(transfer_id, item_id)
        if item is None or not item.is_dir:
            raise ValueError("Manifest directory not found")
        item.scan_complete = True

    async def amark_transfer_manifest_item_started(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        destination_existed_before: bool,
    ) -> None:
        item = self._manifest_item(transfer_id, item_id)
        if item is None or item.is_dir:
            raise ValueError("Manifest file not found")
        if not item.copy_started:
            item.copy_started = True
            item.destination_existed_before = destination_existed_before

    async def aupdate_transfer_manifest_item_size(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID, *, size_bytes: int
    ) -> None:
        item = self._manifest_item(transfer_id, item_id)
        transfer = self.transfers.get(transfer_id)
        if item is None or item.is_dir or transfer is None or size_bytes < 0:
            raise ValueError("Manifest file size cannot be updated")
        if item.copy_started:
            raise ValueError("Manifest file size cannot change after a copy starts")
        if item.size_bytes is not None and item.size_bytes != size_bytes:
            raise ValueError("Manifest file size is already fixed")
        if item.size_bytes is None:
            item.size_bytes = size_bytes
            transfer.bytes_total += size_bytes

    async def aupdate_transfer_manifest_destination(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        destination_path: str,
        requested_destination_path: str,
        rename_reason: str | None,
    ) -> None:
        item = self._manifest_item(transfer_id, item_id)
        if item is None or item.copy_started:
            raise ValueError("Manifest destination can only change before a copy starts")
        item.destination_path = destination_path
        item.requested_destination_path = requested_destination_path
        item.rename_reason = rename_reason

    async def alist_transfer_manifest_items(
        self,
        transfer_id: uuid.UUID,
        statuses: set[str] | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 200,
        include_directories: bool = False,
    ) -> list[TransferManifestItemRecord]:
        allowed_statuses = statuses or {"PENDING"}
        rows = [
            item
            for (item_transfer_id, _), item in self.transfer_manifest_items.items()
            if item_transfer_id == transfer_id
            and (include_directories or not item.is_dir)
            and item.status in allowed_statuses
            and (after_id is None or item.id.int > after_id.int)
        ]
        return sorted(rows, key=lambda item: item.id.int)[:limit]

    async def acount_transfer_manifest_basename_matches(
        self, transfer_id: uuid.UUID, basename: str
    ) -> int:
        return sum(
            1
            for (candidate_transfer_id, _), item in self.transfer_manifest_items.items()
            if candidate_transfer_id == transfer_id
            and not item.is_dir
            and PurePosixPath(item.source_path).name == basename
        )

    async def aupdate_transfer_manifest_item(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        status: str,
        verified_by: str | None = None,
        error_code: str | None = None,
        failure_stage: str | None = None,
        failure_attempt: int | None = None,
    ) -> None:
        if status not in {"COPIED", "SKIPPED", "FAILED", "PENDING"}:
            raise ValueError("Invalid manifest result")
        item = self._manifest_item(transfer_id, item_id)
        transfer = self.transfers.get(transfer_id)
        if item is None or item.is_dir or transfer is None:
            raise ValueError("Manifest file not found")
        old_status = item.status
        if (
            old_status == status
            and item.verified_by == verified_by
            and item.error_code == error_code
            and (status != "FAILED" or item.last_failure_stage == failure_stage)
            and (status != "FAILED" or item.last_failure_attempt == failure_attempt)
        ):
            return
        terminal = {"COPIED", "SKIPPED", "FAILED"}
        if old_status not in terminal and status in terminal:
            transfer.files_completed += 1
        elif old_status in terminal and status not in terminal:
            transfer.files_completed = max(0, transfer.files_completed - 1)
        was_copied = old_status == "COPIED"
        becomes_copied = status == "COPIED"
        if was_copied != becomes_copied:
            transfer.bytes_transferred = max(
                0,
                transfer.bytes_transferred
                + ((item.size_bytes or 0) if becomes_copied else -(item.size_bytes or 0)),
            )
        if old_status == "FAILED":
            transfer.error_count = max(0, transfer.error_count - 1)
        if old_status in {"FAILED", "SKIPPED"}:
            transfer.warning_count = max(0, transfer.warning_count - 1)
        if status == "FAILED":
            transfer.error_count += 1
        if status in {"FAILED", "SKIPPED"}:
            transfer.warning_count += 1
        item.status = status
        item.verified_by = verified_by
        item.error_code = error_code
        if status == "FAILED":
            item.last_failure_stage = failure_stage
            item.last_failure_attempt = failure_attempt
        elif status in {"COPIED", "PENDING"}:
            item.last_failure_stage = None
            item.last_failure_attempt = None
        # For SMB manifests, warning_count tracks failed and skipped file outcomes.
        counts = {
            "copied": max(0, transfer.files_completed - transfer.warning_count),
            "failed": transfer.error_count,
            "skipped": max(0, transfer.warning_count - transfer.error_count),
            "pending": max(0, transfer.files_total - transfer.files_completed),
        }
        transfer.progress_percent = smb_manifest_progress_percent(
            bytes_transferred=transfer.bytes_transferred,
            bytes_total=transfer.bytes_total,
            files_total=transfer.files_total,
            counts=counts,
        )

    async def aget_transfer_manifest_counts(self, transfer_id: uuid.UUID) -> dict[str, int]:
        rows = [
            item
            for (item_transfer_id, _), item in self.transfer_manifest_items.items()
            if item_transfer_id == transfer_id
        ]
        return {
            "total": len(rows),
            "pending": sum(
                1
                for item in rows
                if item.status == "PENDING" or (item.is_dir and not item.scan_complete)
            ),
            "copied": sum(1 for item in rows if item.status == "COPIED"),
            "skipped": sum(1 for item in rows if item.status == "SKIPPED"),
            "failed": sum(1 for item in rows if item.status == "FAILED"),
        }

    async def alist_transfer_manifest_summaries(
        self, transfer_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        summaries = {
            transfer_id: {
                "copied": 0,
                "failed": 0,
                "skipped": 0,
                "pending": 0,
                "renamed": 0,
                "omitted": 0,
                "omitted_bytes": 0,
            }
            for transfer_id in transfer_ids
        }
        status_keys = {
            "COPIED": "copied",
            "FAILED": "failed",
            "SKIPPED": "skipped",
            "PENDING": "pending",
        }
        for (transfer_id, _), item in self.transfer_manifest_items.items():
            key = status_keys.get(item.status)
            summary = summaries.get(transfer_id)
            if summary is not None and item.rename_reason is not None:
                summary["renamed"] += 1
            if summary is not None and not item.is_dir and key is not None:
                summary[key] += 1
                if item.omission_kind is not None:
                    summary["omitted"] += 1
                    summary["omitted_bytes"] += item.size_bytes or 0
        return summaries

    def _manifest_item(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID
    ) -> TransferManifestItemRecord | None:
        return next(
            (
                item
                for (candidate_transfer, _), item in self.transfer_manifest_items.items()
                if candidate_transfer == transfer_id and item.id == item_id
            ),
            None,
        )

    async def aadd_transfer_event(
        self,
        transfer_job_id: uuid.UUID,
        *,
        level: str,
        event_type: str,
        message: str,
        technical_details: dict[str, Any] | None = None,
        provider_error_code: str | None = None,
        item_path: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        self.transfer_events.setdefault(transfer_job_id, []).append(
            {
                "level": level,
                "event_type": event_type,
                "message": message,
                "technical_details": sanitize_data(technical_details),
                "provider_error_code": provider_error_code,
                "item_path": item_path,
                "correlation_id": str(correlation_id or transfer_job_id),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def alist_transfer_events(self, transfer_job_id: uuid.UUID) -> list[dict[str, Any]]:
        return list(self.transfer_events.get(transfer_job_id, []))

    async def aadd_audit_event(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        result: str,
        resource_id: str | None = None,
        metadata: dict[str, Any],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.audit_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
                "metadata": sanitize_data(metadata),
                "ip_address": ip_address,
                "user_agent": user_agent,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def alist_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(reversed(self.audit_events[-limit:]))
