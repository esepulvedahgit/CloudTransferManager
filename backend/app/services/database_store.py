from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, cast

from sqlalchemy import and_, case, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased

from app.models import (
    AuditEvent,
    Base,
    SMTPSettings,
    StorageConnection,
    TransferCounter,
    TransferEvaluation,
    TransferEvent,
    TransferJob,
    TransferManifestItem,
    TransferNotificationOutbox,
    User,
    UserConnectionGrant,
)
from app.security.sanitize import sanitize_data
from app.services.memory_store import (
    ACTIVE_TRANSFER_STATUSES,
    ConnectionInUseError,
    ConnectionRecord,
    ConnectionUnavailableError,
    IdempotencyConflictError,
    SMTPSettingsRecord,
    TransferManifestItemRecord,
    TransferRecord,
    UserAccount,
)
from app.services.source_inventory import InventoryProgress
from app.services.transfer_evaluation import (
    EvaluationClaim,
    EvaluationConflictError,
    EvaluationRecord,
    restart_evaluation,
)
from app.services.transfer_omissions import (
    OmissionKind,
    TransferRecoveryConflictError,
    TransferRecoveryNotFoundError,
    can_skip_manifest_item,
    normalize_source_file_error_policy,
    parent_omissions_request_hash,
    recovery_request_hash,
    resolve_failure_evidence,
    retry_omissions_request_hash,
    skip_unavailable_reason,
)
from app.services.transfer_progress import smb_manifest_progress_percent
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
from app.settings import Settings, get_settings

_TERMINAL_TRANSFER_STATUSES = frozenset(
    {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}
)
_TRANSFER_STATUS_RANK = {
    "SCHEDULED": 0,
    "QUEUED": 1,
    "VALIDATING": 2,
    "RUNNING": 3,
    "RETRYING": 3,
    "CANCELLING": 4,
    "CANCELLED": 5,
    "COMPLETED": 5,
    "COMPLETED_WITH_WARNINGS": 5,
    "FAILED": 5,
}
_NOTIFICATION_LEASE_SECONDS = 300
_EVALUATION_LEASE_SECONDS = 60
_NO_EVALUATION_OVERRIDE = object()


class DatabaseStore:
    """PostgreSQL-backed implementation of the application repository."""

    def __init__(self, settings: Settings | None = None) -> None:
        config = settings or get_settings()
        self.settings = config
        self.engine = create_async_engine(config.resolved_database_url(), pool_pre_ping=True)
        self.sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_schema(self) -> None:
        """Create schema for isolated tests; deployments use Alembic migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def _ensure_notification_outbox(
        self, session: AsyncSession, transfer: TransferJob
    ) -> None:
        if (
            not transfer.send_email_notification
            or transfer.status not in _TERMINAL_TRANSFER_STATUSES
        ):
            return
        user = await session.get(User, transfer.requested_by)
        source = await session.get(StorageConnection, transfer.source_connection_id)
        destination = await session.get(StorageConnection, transfer.destination_connection_id)
        if user is None or source is None or destination is None:
            return
        omitted_count, omitted_bytes = (
            await session.execute(
                select(
                    func.count(TransferManifestItem.id),
                    func.coalesce(func.sum(TransferManifestItem.size_bytes), 0),
                ).where(
                    TransferManifestItem.transfer_job_id == transfer.id,
                    TransferManifestItem.is_dir.is_(False),
                    TransferManifestItem.omission_kind.is_not(None),
                )
            )
        ).one()
        dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(TransferNotificationOutbox).values(
            id=uuid.uuid4(),
            transfer_job_id=transfer.id,
            transfer_attempt=transfer.attempt_count,
            status="PENDING",
            attempt_count=0,
            snapshot={
                "public_id": transfer.public_id,
                "status": transfer.status,
                "bytes_transferred": transfer.bytes_transferred,
                "omitted": int(omitted_count or 0),
                "omitted_bytes": int(omitted_bytes or 0),
                "created_at": transfer.created_at.isoformat(),
                "completed_at": (transfer.completed_at or datetime.now(UTC)).isoformat(),
                "recipient": user.email,
                "source_name": source.name,
                "destination_name": destination.name,
            },
            last_error_code=None,
        )
        await session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    TransferNotificationOutbox.transfer_job_id,
                    TransferNotificationOutbox.transfer_attempt,
                ]
            )
        )

    async def aensure_transfer_notification_outbox(self, transfer_id: uuid.UUID) -> None:
        """Materialize a legacy queued notification as durable database state."""
        async with self.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer_id).with_for_update()
                )
                if transfer is not None:
                    await self._ensure_notification_outbox(session, transfer)

    async def adispatch_pending_notifications(
        self,
        now: datetime,
        limit: int = 100,
        lease_seconds: int = _NOTIFICATION_LEASE_SECONDS,
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        now = now.astimezone(UTC)
        expired_claim = now - timedelta(seconds=lease_seconds)
        async with self.sessions() as session:
            async with session.begin():
                rows = list(
                    await session.scalars(
                        select(TransferNotificationOutbox)
                        .where(
                            or_(
                                and_(
                                    TransferNotificationOutbox.status == "PENDING",
                                    TransferNotificationOutbox.next_attempt_at <= now,
                                ),
                                and_(
                                    TransferNotificationOutbox.status.in_({"QUEUED", "SENDING"}),
                                    TransferNotificationOutbox.claimed_at < expired_claim,
                                ),
                            )
                        )
                        .order_by(TransferNotificationOutbox.created_at)
                        .limit(max(1, min(limit, 500)))
                        .with_for_update(skip_locked=True)
                    )
                )
                dispatch_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
                for row in rows:
                    if row.attempt_count >= 5:
                        row.status = "FAILED"
                        row.last_error_code = "ATTEMPTS_EXHAUSTED"
                        row.claimed_at = None
                        row.claim_token = None
                        continue
                    row.status = "QUEUED"
                    row.claimed_at = now
                    dispatch_token = uuid.uuid4()
                    row.claim_token = dispatch_token
                    dispatch_ids.append((row.id, dispatch_token))
                return dispatch_ids

    async def arelease_notification_dispatch(
        self, notification_id: uuid.UUID, now: datetime, dispatch_token: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferNotificationOutbox)
                    .where(
                        TransferNotificationOutbox.id == notification_id,
                        TransferNotificationOutbox.status == "QUEUED",
                        TransferNotificationOutbox.claim_token == dispatch_token,
                    )
                    .with_for_update()
                )
                if row is None:
                    return
                row.status = "PENDING"
                row.claimed_at = None
                row.claim_token = None
                row.next_attempt_at = now.astimezone(UTC) + timedelta(seconds=15)
                row.last_error_code = "QUEUE_UNAVAILABLE"

    async def aclaim_notification_delivery(
        self,
        notification_id: uuid.UUID,
        now: datetime,
        dispatch_token: uuid.UUID,
        lease_seconds: int = _NOTIFICATION_LEASE_SECONDS,
    ) -> tuple[dict[str, Any], uuid.UUID] | None:
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferNotificationOutbox)
                    .where(TransferNotificationOutbox.id == notification_id)
                    .with_for_update()
                )
                if (
                    row is None
                    or row.status != "QUEUED"
                    or row.claim_token != dispatch_token
                    or row.claimed_at is None
                ):
                    return None
                claimed_at = row.claimed_at
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                if claimed_at <= now - timedelta(seconds=lease_seconds):
                    return None
                if row.attempt_count >= 5:
                    row.status = "FAILED"
                    row.last_error_code = "ATTEMPTS_EXHAUSTED"
                    row.claimed_at = None
                    row.claim_token = None
                    return None
                row.status = "SENDING"
                row.attempt_count += 1
                row.claimed_at = now
                row.claim_token = uuid.uuid4()
                return dict(row.snapshot), row.claim_token

    async def amark_notification_sent(
        self, notification_id: uuid.UUID, claim_token: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferNotificationOutbox)
                    .where(TransferNotificationOutbox.id == notification_id)
                    .with_for_update()
                )
                if row is not None and row.status == "SENDING" and row.claim_token == claim_token:
                    row.status = "SENT"
                    row.claimed_at = None
                    row.claim_token = None
                    row.last_error_code = None

    async def afail_notification_delivery(
        self,
        notification_id: uuid.UUID,
        now: datetime,
        claim_token: uuid.UUID,
        error_code: str = "SMTP_DELIVERY_FAILED",
    ) -> None:
        safe_codes = {"SMTP_CONFIGURATION_INVALID", "SMTP_DELIVERY_FAILED"}
        error_code = error_code if error_code in safe_codes else "SMTP_DELIVERY_FAILED"
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferNotificationOutbox)
                    .where(TransferNotificationOutbox.id == notification_id)
                    .with_for_update()
                )
                if row is None or row.status != "SENDING" or row.claim_token != claim_token:
                    return
                row.claimed_at = None
                row.claim_token = None
                row.last_error_code = error_code
                if row.attempt_count >= 5:
                    row.status = "FAILED"
                else:
                    row.status = "PENDING"
                    row.next_attempt_at = now + timedelta(
                        seconds=min(3600, 15 * (2 ** (row.attempt_count - 1)))
                    )

    async def alist_notification_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(TransferNotificationOutbox, TransferJob.public_id)
                    .join(TransferJob, TransferJob.id == TransferNotificationOutbox.transfer_job_id)
                    .order_by(TransferNotificationOutbox.created_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            ).all()
            return [
                {
                    "id": str(outbox.id),
                    "transfer_public_id": public_id,
                    "transfer_attempt": outbox.transfer_attempt,
                    "status": outbox.status,
                    "attempt_count": outbox.attempt_count,
                    "last_error_code": outbox.last_error_code,
                    "next_attempt_at": outbox.next_attempt_at.isoformat(),
                    "created_at": outbox.created_at.isoformat(),
                }
                for outbox, public_id in rows
            ]

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def acheck(self) -> bool:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    @asynccontextmanager
    async def transfer_execution_lock(self, transfer_id: uuid.UUID) -> AsyncIterator[bool]:
        """Hold a PostgreSQL session lock for the full lifetime of one worker task."""
        async with self.engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                yield True
                return

            lock_key = self._transfer_lock_key(transfer_id)
            lock_parameters = {"lock_key": lock_key}
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"), lock_parameters
                )
            )
            await connection.commit()
            if not acquired:
                yield False
                return

            try:
                yield True
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), lock_parameters
                )
                await connection.commit()

    @staticmethod
    def _transfer_lock_key(transfer_id: uuid.UUID) -> int:
        return int.from_bytes(hashlib.sha256(transfer_id.bytes).digest()[:8], "big", signed=True)

    @staticmethod
    def _smtp_settings(row: SMTPSettings) -> SMTPSettingsRecord:
        return SMTPSettingsRecord(
            id=row.id,
            host=row.host,
            port=row.port,
            use_tls=row.use_tls,
            from_address=row.from_address,
            encrypted_credentials=bytes(row.encrypted_credentials),
        )

    async def aget_smtp_settings(self) -> SMTPSettingsRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(select(SMTPSettings).where(SMTPSettings.scope == "global"))
            return self._smtp_settings(row) if row else None

    async def acreate_smtp_settings_if_absent(
        self, settings: SMTPSettingsRecord
    ) -> SMTPSettingsRecord:
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(SMTPSettings).values(
                id=settings.id,
                scope="global",
                host=settings.host,
                port=settings.port,
                use_tls=settings.use_tls,
                from_address=settings.from_address,
                encrypted_credentials=settings.encrypted_credentials,
            )
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=[SMTPSettings.scope])
            )
            row = await session.scalar(select(SMTPSettings).where(SMTPSettings.scope == "global"))
            if row is None:
                raise RuntimeError("SMTP settings reservation failed")
            await session.commit()
            return self._smtp_settings(row)

    async def asave_smtp_settings(self, settings: SMTPSettingsRecord) -> SMTPSettingsRecord:
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(SMTPSettings).values(
                id=settings.id,
                scope="global",
                host=settings.host,
                port=settings.port,
                use_tls=settings.use_tls,
                from_address=settings.from_address,
                encrypted_credentials=settings.encrypted_credentials,
            )
            upsert = statement.on_conflict_do_update(
                index_elements=[SMTPSettings.scope],
                set_={
                    "host": settings.host,
                    "port": settings.port,
                    "use_tls": settings.use_tls,
                    "from_address": settings.from_address,
                    "encrypted_credentials": settings.encrypted_credentials,
                },
            ).returning(SMTPSettings)
            row = (await session.scalars(upsert)).one()
            await session.commit()
            return self._smtp_settings(row)

    @staticmethod
    def _user(row: User) -> UserAccount:
        return UserAccount(
            id=row.id,
            username=row.username,
            email=row.email,
            password_hash=row.password_hash,
            role=row.role,
            is_active=row.is_active,
            must_change_password=row.must_change_password,
            failed_login_attempts=row.failed_login_attempts,
            locked_until=row.locked_until,
            last_login_at=row.last_login_at,
        )

    @staticmethod
    def _connection(row: StorageConnection) -> ConnectionRecord:
        return ConnectionRecord(
            id=row.id,
            name=row.name,
            provider_type=row.provider_type,
            adapter_type=row.adapter_type,
            created_by=row.created_by,
            encrypted_credentials=bytes(row.encrypted_credentials),
            status=row.status,
            credential_version=row.credential_version,
            last_tested_at=row.last_tested_at,
            last_test_status=row.last_test_status,
            is_global=row.is_global,
            deleted_at=row.deleted_at,
        )

    @staticmethod
    def _transfer(
        row: TransferJob,
        *,
        evaluation_override: TransferEvaluation | None | object = _NO_EVALUATION_OVERRIDE,
    ) -> TransferRecord:
        if evaluation_override is _NO_EVALUATION_OVERRIDE:
            evaluation_row = row.evaluation
        else:
            evaluation_row = cast(TransferEvaluation | None, evaluation_override)
        return TransferRecord(
            id=row.id,
            public_id=row.public_id,
            requested_by=row.requested_by,
            source_connection_id=row.source_connection_id,
            destination_connection_id=row.destination_connection_id,
            source_credential_version=row.source_credential_version,
            destination_credential_version=row.destination_credential_version,
            source_paths=list(row.source_paths),
            source_item_ids=list(row.source_item_ids) if row.source_item_ids is not None else None,
            source_parent_ids=(
                list(row.source_parent_ids) if row.source_parent_ids is not None else None
            ),
            destination_path=row.destination_path,
            conflict_policy=row.conflict_policy,
            send_email_notification=row.send_email_notification,
            status=row.status,
            scheduled_at=row.scheduled_at,
            schedule_timezone=row.schedule_timezone,
            execution_mode=row.execution_mode,
            depends_on_transfer_id=row.depends_on_transfer_id,
            source_file_error_policy=row.source_file_error_policy,
            accepted_parent_omissions_attempt=row.accepted_parent_omissions_attempt,
            wait_reason=row.wait_reason,
            dispatch_token=row.dispatch_token,
            dispatch_claimed_at=row.dispatch_claimed_at,
            execution_started_at=row.execution_started_at,
            execution_heartbeat_at=row.execution_heartbeat_at,
            cancel_requested_at=row.cancel_requested_at,
            progress_percent=float(row.progress_percent or 0),
            files_total=row.files_total or 0,
            files_completed=row.files_completed,
            bytes_total=row.bytes_total or 0,
            bytes_transferred=row.bytes_transferred,
            current_item=row.current_item,
            average_speed_bps=row.average_speed_bps,
            current_speed_bps=row.current_speed_bps,
            estimated_remaining_seconds=row.estimated_remaining_seconds,
            attempt_count=row.attempt_count,
            error_count=row.error_count,
            warning_count=row.warning_count,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            evaluation=(
                EvaluationRecord.from_row(evaluation_row)
                if evaluation_row is not None
                else None
            ),
        )

    @staticmethod
    def _manifest_item(row: TransferManifestItem) -> TransferManifestItemRecord:
        return TransferManifestItemRecord(
            id=row.id,
            transfer_id=row.transfer_job_id,
            source_path=row.source_path,
            source_object_id=row.source_object_id,
            destination_path=row.destination_path,
            requested_destination_path=row.requested_destination_path or row.destination_path,
            rename_reason=row.rename_reason,
            is_dir=row.is_dir,
            size_bytes=row.size_bytes,
            modified_at=row.modified_at,
            scan_complete=row.scan_complete,
            status=row.status,
            copy_started=row.copy_started,
            destination_existed_before=row.destination_existed_before,
            error_code=row.error_code,
            verified_by=row.verified_by,
            omission_kind=row.omission_kind,
            omitted_at=row.omitted_at,
            omitted_by=row.omitted_by,
            last_failure_stage=row.last_failure_stage,
            last_failure_attempt=row.last_failure_attempt,
        )

    async def acreate_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        async with self.sessions() as session:
            row = User(
                username=username,
                email=email,
                password_hash=password_hash,
                role=role,
                is_active=is_active,
                must_change_password=must_change_password,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._user(row)

    async def aget_user(self, user_id: uuid.UUID) -> UserAccount | None:
        async with self.sessions() as session:
            row = await session.get(User, user_id)
            return self._user(row) if row else None

    async def alist_users(self) -> list[UserAccount]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(User).order_by(User.username))).all()
            return [self._user(row) for row in rows]

    async def acount_active_admins(self) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.is_active.is_(True), User.role == "ADMIN")
                )
                or 0
            )

    async def afind_user(self, username_or_email: str) -> UserAccount | None:
        normalized = username_or_email.casefold()
        async with self.sessions() as session:
            row = await session.scalar(
                select(User).where(
                    or_(
                        func.lower(User.username) == normalized,
                        func.lower(User.email) == normalized,
                    )
                )
            )
            return self._user(row) if row else None

    async def arecord_login_failure(
        self, user_id: uuid.UUID, expected_password_hash: str, now: datetime
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(User)
                    .where(User.id == user_id, User.password_hash == expected_password_hash)
                    .with_for_update()
                )
                if row is None:
                    return
                row.failed_login_attempts += 1
                if row.failed_login_attempts >= 5:
                    row.locked_until = now + timedelta(minutes=15)

    async def arecord_login_success(
        self, user_id: uuid.UUID, expected_password_hash: str, now: datetime
    ) -> UserAccount | None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(User)
                    .where(
                        User.id == user_id,
                        User.password_hash == expected_password_hash,
                        User.is_active.is_(True),
                        or_(User.locked_until.is_(None), User.locked_until <= now),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                row.failed_login_attempts = 0
                row.locked_until = None
                row.last_login_at = now
                await session.flush()
                return self._user(row)

    async def aupdate_user_password(
        self,
        user_id: uuid.UUID,
        password_hash: str,
        *,
        must_change_password: bool,
        reset_login_failures: bool = False,
        expected_password_hash: str | None = None,
    ) -> UserAccount | None:
        async with self.sessions() as session:
            async with session.begin():
                statement = select(User).where(User.id == user_id)
                if expected_password_hash is not None:
                    statement = statement.where(User.password_hash == expected_password_hash)
                row = await session.scalar(statement.with_for_update())
                if row is None:
                    return None
                row.password_hash = password_hash
                row.must_change_password = must_change_password
                if reset_login_failures:
                    row.failed_login_attempts = 0
                    row.locked_until = None
                await session.flush()
                return self._user(row)

    async def aupdate_user_profile(
        self, user_id: uuid.UUID, changes: dict[str, str | bool]
    ) -> tuple[UserAccount | None, bool]:
        allowed = {"username", "email", "role", "is_active"}
        if set(changes) - allowed:
            raise ValueError("Unsupported user profile field")
        async with self.sessions() as session:
            async with session.begin():
                active_admins: list[User] = []
                if {"role", "is_active"} & changes.keys():
                    if session.bind is not None and session.bind.dialect.name == "postgresql":
                        await session.execute(
                            text("SELECT pg_advisory_xact_lock(:lock_key)"),
                            {"lock_key": 0x43544D41444D494E},
                        )
                    active_admins = list(
                        (
                            await session.scalars(
                                select(User)
                                .where(User.is_active.is_(True), User.role == "ADMIN")
                                .order_by(User.id)
                                .with_for_update()
                            )
                        ).all()
                    )
                row = await session.scalar(select(User).where(User.id == user_id).with_for_update())
                if row is None:
                    return None, False
                removes_admin = (
                    row.is_active
                    and row.role == "ADMIN"
                    and (changes.get("is_active", True) is False or changes.get("role") == "USER")
                )
                if removes_admin and len(active_admins) <= 1:
                    return None, True
                for field_name, value in changes.items():
                    setattr(row, field_name, value)
                await session.flush()
                return self._user(row), False

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
        async with self.sessions() as session:
            row = StorageConnection(
                id=connection_id,
                name=name,
                provider_type=provider_type,
                adapter_type=adapter_type,
                created_by=created_by,
                encrypted_credentials=encrypted_credentials,
                is_global=is_global,
                status="ACTIVE",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._connection(row)

    async def aget_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(StorageConnection).where(
                    StorageConnection.id == connection_id,
                    StorageConnection.deleted_at.is_(None),
                )
            )
            return self._connection(row) if row else None

    async def aarchive_connection(
        self,
        connection_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectionRecord | None:
        async with self.sessions() as session:
            async with session.begin():
                connection = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update()
                )
                if connection is None or connection.deleted_at is not None:
                    return None
                active_transfer = await session.scalar(
                    select(TransferJob.id)
                    .where(
                        TransferJob.status.in_(ACTIVE_TRANSFER_STATUSES),
                        or_(
                            TransferJob.source_connection_id == connection_id,
                            TransferJob.destination_connection_id == connection_id,
                        ),
                    )
                    .limit(1)
                )
                if active_transfer is not None:
                    raise ConnectionInUseError("Connection has an active transfer")
                connection.deleted_at = datetime.now(UTC)
                connection.encrypted_credentials = b""
                await session.execute(
                    delete(UserConnectionGrant).where(
                        UserConnectionGrant.connection_id == connection_id
                    )
                )
                session.add(
                    AuditEvent(
                        actor_user_id=actor_user_id,
                        action="CONNECTION_ARCHIVED",
                        resource_type="connection",
                        resource_id=str(connection.id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(
                            {
                                "provider_type": connection.provider_type,
                                "is_global": connection.is_global,
                            }
                        ),
                        ip_address=ip_address,
                        user_agent=user_agent[:255] if user_agent else None,
                    )
                )
            return self._connection(connection)

    async def alist_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        async with self.sessions() as session:
            statement = (
                select(StorageConnection)
                .where(StorageConnection.deleted_at.is_(None))
                .order_by(StorageConnection.name)
            )
            if user_id is not None:
                granted = select(UserConnectionGrant.connection_id).where(
                    UserConnectionGrant.user_id == user_id
                )
                statement = statement.where(
                    or_(StorageConnection.is_global.is_(True), StorageConnection.id.in_(granted))
                )
            rows = (await session.scalars(statement)).all()
            return [self._connection(row) for row in rows]

    async def ahas_connection_access(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        async with self.sessions() as session:
            connection = await session.get(StorageConnection, connection_id)
            if connection is None or connection.deleted_at is not None:
                return False
            if connection.is_global:
                return True
            return await session.get(UserConnectionGrant, (user_id, connection_id)) is not None

    async def agrant_connection(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, granted_by: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                connection = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update()
                )
                if connection is None or connection.deleted_at is not None:
                    raise ConnectionUnavailableError("not_found")
                existing = await session.get(UserConnectionGrant, (user_id, connection_id))
                if existing is None:
                    session.add(
                        UserConnectionGrant(
                            user_id=user_id, connection_id=connection_id, granted_by=granted_by
                        )
                    )

    async def arevoke_connection(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            grant = await session.get(UserConnectionGrant, (user_id, connection_id))
            if grant is not None:
                await session.delete(grant)
                await session.commit()

    async def asave_connection(self, connection: ConnectionRecord) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection.id)
                    .with_for_update()
                )
                if row is None or row.deleted_at is not None:
                    raise ConnectionUnavailableError("not_found")
                row.name = connection.name
                row.adapter_type = connection.adapter_type
                row.status = connection.status
                row.last_tested_at = connection.last_tested_at
                row.last_test_status = connection.last_test_status
                row.is_global = connection.is_global
                if bytes(row.encrypted_credentials) != connection.encrypted_credentials:
                    row.credential_version += 1
                row.encrypted_credentials = connection.encrypted_credentials
                connection.credential_version = row.credential_version

    async def _next_public_id(self, session: AsyncSession) -> str:
        year = datetime.now(UTC).year
        dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
        statement: Any
        if dialect == "postgresql":
            statement = pg_insert(TransferCounter).values(year=year, last_value=1)
        else:
            statement = sqlite_insert(TransferCounter).values(year=year, last_value=1)
        statement = statement.on_conflict_do_update(
            index_elements=[TransferCounter.year],
            set_={"last_value": TransferCounter.last_value + 1},
        ).returning(TransferCounter.last_value)
        number = await session.scalar(statement)
        return f"TRF-{year}-{int(number or 1):06d}"

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
        source_file_error_policy = normalize_source_file_error_policy(source_file_error_policy)
        if (idempotency_key is None) != (idempotency_request_hash is None):
            raise ValueError("Idempotency key and request hash must be supplied together")
        async with self.sessions() as session:
            connection_ids = sorted({source_connection_id, destination_connection_id})
            try:
                async with session.begin():
                    if idempotency_key is not None:
                        existing = await session.scalar(
                            select(TransferJob).where(
                                TransferJob.requested_by == requested_by,
                                TransferJob.idempotency_key == idempotency_key,
                            )
                        )
                        if existing is not None:
                            if existing.idempotency_request_hash != idempotency_request_hash:
                                raise IdempotencyConflictError(
                                    "Idempotency key was already used for different input"
                                )
                            replay = self._transfer(existing)
                            replay.idempotent_replay = True
                            return replay

                    connections = (
                        await session.scalars(
                            select(StorageConnection)
                            .where(StorageConnection.id.in_(connection_ids))
                            .order_by(StorageConnection.id)
                            .with_for_update()
                        )
                    ).all()
                    by_id = {connection.id: connection for connection in connections}
                    for connection_id in connection_ids:
                        connection = by_id.get(connection_id)
                        if connection is None or connection.deleted_at is not None:
                            raise ConnectionUnavailableError("not_found")
                        if connection.status != "ACTIVE":
                            raise ConnectionUnavailableError("inactive")
                    row = TransferJob(
                        id=uuid.uuid4(),
                        public_id=await self._next_public_id(session),
                        requested_by=requested_by,
                        idempotency_key=idempotency_key,
                        idempotency_request_hash=idempotency_request_hash,
                        source_connection_id=source_connection_id,
                        destination_connection_id=destination_connection_id,
                        source_paths=source_paths,
                        source_item_ids=source_item_ids,
                        source_parent_ids=source_parent_ids,
                        destination_path=destination_path,
                        operation="COPY",
                        conflict_policy=conflict_policy,
                        send_email_notification=send_email_notification,
                        status=(
                            "SCHEDULED"
                            if scheduled_at is not None and scheduled_at > datetime.now(UTC)
                            else "QUEUED"
                        ),
                        scheduled_at=scheduled_at,
                        schedule_timezone=schedule_timezone,
                        execution_mode=execution_mode,
                        depends_on_transfer_id=depends_on_transfer_id,
                        source_file_error_policy=source_file_error_policy,
                        wait_reason=(
                            "WAITING_UNTIL"
                            if scheduled_at is not None and scheduled_at > datetime.now(UTC)
                            else None
                        ),
                        source_credential_version=by_id[source_connection_id].credential_version,
                        destination_credential_version=by_id[
                            destination_connection_id
                        ].credential_version,
                        created_at=datetime.now(UTC),
                    )
                    session.add(row)
                    evaluation_row: TransferEvaluation | None = None
                    if scheduled_at is not None and scheduled_at > datetime.now(UTC):
                        is_smb_source = by_id[source_connection_id].provider_type == "SMB"
                        evaluation_row = TransferEvaluation(
                            id=uuid.uuid4(),
                            transfer_job_id=row.id,
                            status="PENDING",
                            generation=1,
                            sequence=0,
                            count_complete=False,
                            max_files=(
                                self.settings.smb_transfer_max_files
                                if is_smb_source
                                else self.settings.transfer_max_files
                            ),
                            source_credential_version=by_id[
                                source_connection_id
                            ].credential_version,
                            destination_credential_version=by_id[
                                destination_connection_id
                            ].credential_version,
                            attempt_count=0,
                        )
                        row.evaluation = evaluation_row
                        session.add(evaluation_row)
                    await session.flush()
                    if audit_event is not None:
                        event_metadata = dict(audit_event.get("metadata") or {})
                        event_metadata.setdefault("public_id", row.public_id)
                        session.add(
                            AuditEvent(
                                actor_user_id=requested_by,
                                action="TRANSFER_CREATED",
                                resource_type="transfer",
                                resource_id=str(row.id),
                                result="SUCCESS",
                                event_metadata=sanitize_data(event_metadata),
                                ip_address=audit_event.get("ip_address"),
                                user_agent=(
                                    str(audit_event["user_agent"])[:255]
                                    if audit_event.get("user_agent")
                                    else None
                                ),
                            )
                        )
            except IntegrityError:
                if idempotency_key is None:
                    raise
                await session.rollback()
                recovered = await self.aget_transfer_by_idempotency(
                    requested_by, idempotency_key, idempotency_request_hash or ""
                )
                if recovered is None:
                    raise
                recovered.idempotent_replay = True
                return recovered
            await session.refresh(row)
            transfer = self._transfer(row)
            if evaluation_row is not None:
                transfer.evaluation = EvaluationRecord.from_row(evaluation_row)
            return transfer

    async def arequest_transfer_evaluation(
        self, transfer_id: uuid.UUID, now: datetime
    ) -> EvaluationRecord:
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == transfer_id)
                    .with_for_update()
                )
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
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == transfer_id)
                    .with_for_update()
                )
                if evaluation is None:
                    raise EvaluationConflictError(
                        "Esta transferencia no tiene una evaluación anticipada para repetir."
                    )
                if evaluation.status in {"PENDING", "EVALUATING", "WAITING_PARENT"}:
                    return EvaluationRecord.from_row(evaluation)

                restart_evaluation(evaluation, now=now)
                parent_wait = None
                if transfer.depends_on_transfer_id is not None:
                    parent = await session.get(TransferJob, transfer.depends_on_transfer_id)
                    parent_omitted_count = int(
                        await session.scalar(
                            select(func.count()).where(
                                TransferManifestItem.transfer_job_id
                                == transfer.depends_on_transfer_id,
                                TransferManifestItem.is_dir.is_(False),
                                TransferManifestItem.omission_kind.is_not(None),
                            )
                        )
                        or 0
                    )
                    parent_wait = dependency_wait_reason(
                        parent.status if parent is not None else "FAILED",
                        parent_omitted_count=parent_omitted_count,
                        parent_attempt_count=parent.attempt_count if parent is not None else 1,
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
                return EvaluationRecord.from_row(evaluation)

    async def aget_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        async with self.sessions() as session:
            row = await session.get(TransferJob, transfer_id)
            return self._transfer(row) if row else None

    async def alist_transfer_evaluations(
        self, transfer_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, EvaluationRecord]:
        if not transfer_ids:
            return {}
        async with self.sessions() as session:
            rows = await session.scalars(
                select(TransferEvaluation).where(
                    TransferEvaluation.transfer_job_id.in_(transfer_ids)
                )
            )
            return {row.transfer_job_id: EvaluationRecord.from_row(row) for row in rows}

    @staticmethod
    def _evaluation_claim_matches(
        evaluation: TransferEvaluation,
        transfer: TransferJob | None,
        claim: EvaluationClaim,
    ) -> bool:
        return bool(
            transfer is not None
            and evaluation.status == "EVALUATING"
            and evaluation.generation == claim.generation
            and evaluation.claim_token == claim.claim_token
            and transfer.status in {"SCHEDULED", "QUEUED"}
            and transfer.dispatch_token is None
            and transfer.execution_started_at is None
            and transfer.cancel_requested_at is None
        )

    async def aclaim_due_evaluations(
        self,
        now: datetime,
        *,
        limit: int = 1,
    ) -> list[EvaluationClaim]:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = now.astimezone(UTC)
        stale_before = now - timedelta(seconds=_EVALUATION_LEASE_SECONDS)
        claims: list[EvaluationClaim] = []
        parent = aliased(TransferJob)
        parent_succeeded = exists(
            select(parent.id).where(
                parent.id == TransferJob.depends_on_transfer_id,
                parent.status.in_({"COMPLETED", "COMPLETED_WITH_WARNINGS"}),
            )
        )
        async with self.sessions() as session:
            async with session.begin():
                rows = list(
                    await session.scalars(
                        select(TransferEvaluation)
                        .join(
                            TransferJob,
                            TransferJob.id == TransferEvaluation.transfer_job_id,
                        )
                        .where(
                            or_(
                                and_(
                                    TransferEvaluation.status == "PENDING",
                                    or_(
                                        TransferEvaluation.retry_at.is_(None),
                                        TransferEvaluation.retry_at <= now,
                                    ),
                                ),
                                and_(
                                    TransferEvaluation.status == "EVALUATING",
                                    or_(
                                        TransferEvaluation.heartbeat_at.is_(None),
                                        TransferEvaluation.heartbeat_at <= stale_before,
                                    ),
                                ),
                                and_(
                                    TransferEvaluation.status == "WAITING_PARENT",
                                    parent_succeeded,
                                ),
                            )
                        )
                        .order_by(TransferEvaluation.updated_at, TransferEvaluation.id)
                        .limit(min(limit, 100))
                        .with_for_update(skip_locked=True)
                    )
                )
                parent_ids = (
                    set(
                        await session.scalars(
                            select(TransferJob.depends_on_transfer_id).where(
                                TransferJob.id.in_([item.transfer_job_id for item in rows]),
                                TransferJob.depends_on_transfer_id.is_not(None),
                            )
                        )
                    )
                    if rows
                    else set()
                )
                omitted_by_parent = (
                    {
                        transfer_id: int(count)
                        for transfer_id, count in (
                            await session.execute(
                                select(
                                    TransferManifestItem.transfer_job_id,
                                    func.count(),
                                )
                                .where(
                                    TransferManifestItem.transfer_job_id.in_(parent_ids),
                                    TransferManifestItem.is_dir.is_(False),
                                    TransferManifestItem.omission_kind.is_not(None),
                                )
                                .group_by(TransferManifestItem.transfer_job_id)
                            )
                        ).all()
                    }
                    if parent_ids
                    else {}
                )
                for evaluation in rows:
                    transfer = await session.scalar(
                        select(TransferJob)
                        .where(TransferJob.id == evaluation.transfer_job_id)
                        .with_for_update()
                    )
                    if (
                        transfer is None
                        or transfer.status not in {"SCHEDULED", "QUEUED"}
                        or transfer.dispatch_token is not None
                        or transfer.execution_started_at is not None
                        or transfer.cancel_requested_at is not None
                    ):
                        evaluation.claim_token = None
                        evaluation.heartbeat_at = None
                        continue

                    if transfer.depends_on_transfer_id is not None:
                        parent_transfer = await session.scalar(
                            select(TransferJob)
                            .where(TransferJob.id == transfer.depends_on_transfer_id)
                            .with_for_update()
                        )
                        parent_wait = dependency_wait_reason(
                            parent_transfer.status if parent_transfer is not None else "FAILED",
                            parent_omitted_count=omitted_by_parent.get(
                                transfer.depends_on_transfer_id, 0
                            ),
                            parent_attempt_count=(
                                parent_transfer.attempt_count if parent_transfer is not None else 1
                            ),
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
                                evaluation.safe_error_code = safe_error_code
                                evaluation.safe_message = safe_message
                                evaluation.sequence = 1
                            elif (
                                evaluation.safe_error_code != safe_error_code
                                or evaluation.safe_message != safe_message
                                or transfer.wait_reason != parent_wait
                            ):
                                evaluation.safe_error_code = safe_error_code
                                evaluation.safe_message = safe_message
                                evaluation.sequence += 1
                                evaluation.updated_at = now
                            transfer.wait_reason = parent_wait
                            continue
                        if evaluation.status == "WAITING_PARENT":
                            restart_evaluation(evaluation, now=now)
                            transfer.wait_reason = "WAITING_EVALUATION"
                    elif evaluation.status == "WAITING_PARENT":
                        restart_evaluation(evaluation, now=now)
                        transfer.wait_reason = "WAITING_EVALUATION"

                    owner = await session.get(User, transfer.requested_by)
                    if owner is None or not owner.is_active:
                        transfer.wait_reason = "WAITING_USER_ACTIVE"
                        continue
                    source = await session.get(StorageConnection, transfer.source_connection_id)
                    destination = await session.get(
                        StorageConnection, transfer.destination_connection_id
                    )
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
                            and evaluation.source_credential_version
                            != source.credential_version
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
                    if owner.role != "ADMIN":
                        granted = set(
                            (
                                await session.scalars(
                                    select(UserConnectionGrant.connection_id).where(
                                        UserConnectionGrant.user_id == owner.id,
                                        UserConnectionGrant.connection_id.in_(
                                            [source.id, destination.id]
                                        ),
                                    )
                                )
                            ).all()
                        )
                        if any(
                            not connection.is_global
                            and connection.id not in granted
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
                        restart_evaluation(
                            evaluation,
                            now=now,
                            max_files=maximum_files,
                        )
                        transfer.wait_reason = "WAITING_EVALUATION"
                        continue

                    if evaluation.status == "EVALUATING" and session.bind is not None:
                        if session.bind.dialect.name == "postgresql":
                            connection = await session.connection()
                            lock_key = self._transfer_lock_key(transfer.id)
                            lock_available = bool(
                                await connection.scalar(
                                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                                    {"lock_key": lock_key},
                                )
                            )
                            if not lock_available:
                                continue
                            await connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_key)"),
                                {"lock_key": lock_key},
                            )

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
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == claim.transfer_id)
                    .with_for_update()
                )
                transfer = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == claim.transfer_id)
                    .with_for_update()
                )
                if (
                    evaluation is None
                    or transfer is None
                    or not self._evaluation_claim_matches(evaluation, transfer, claim)
                ):
                    return False
                evaluation.heartbeat_at = now
                return True

    async def asave_evaluation_progress(
        self, claim: EvaluationClaim, progress: InventoryProgress, now: datetime
    ) -> bool:
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == claim.transfer_id)
                    .with_for_update()
                )
                transfer = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == claim.transfer_id)
                    .with_for_update()
                )
                if evaluation is None or not self._evaluation_claim_matches(
                    evaluation, transfer, claim
                ):
                    return False
                if (
                    evaluation.files_found == progress.files_found
                    and evaluation.count_complete == progress.count_complete
                ):
                    return True
                evaluation.files_found = progress.files_found
                evaluation.count_complete = progress.count_complete
                evaluation.sequence += 1
                evaluation.updated_at = now
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
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == claim.transfer_id)
                    .with_for_update()
                )
                transfer = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == claim.transfer_id)
                    .with_for_update()
                )
                if (
                    evaluation is None
                    or transfer is None
                    or not self._evaluation_claim_matches(evaluation, transfer, claim)
                ):
                    return False
                evaluation.status = status
                evaluation.files_found = progress.files_found
                evaluation.count_complete = progress.count_complete
                evaluation.safe_error_code = error_code
                evaluation.safe_message = message
                evaluation.retry_at = retry_at.astimezone(UTC) if retry_at is not None else None
                evaluation.completed_at = (
                    now if status in {"APPROVED", "BLOCKED", "ERROR"} else None
                )
                evaluation.claim_token = None
                evaluation.heartbeat_at = None
                evaluation.sequence += 1
                evaluation.updated_at = now
                if status == "APPROVED":
                    scheduled_at = transfer.scheduled_at
                    if scheduled_at is not None and scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=UTC)
                    transfer.wait_reason = (
                        "WAITING_UNTIL"
                        if scheduled_at is not None and scheduled_at > now
                        else None
                    )
                elif status in {"BLOCKED", "ERROR"}:
                    transfer.wait_reason = "BLOCKED_EVALUATION"
                elif status == "WAITING_PARENT":
                    transfer.wait_reason = wait_reason or "WAITING_PARENT"
                else:
                    transfer.wait_reason = "WAITING_EVALUATION"
                return True

    async def aget_transfer_by_idempotency(
        self,
        requested_by: uuid.UUID,
        idempotency_key: uuid.UUID,
        request_hash: str,
    ) -> TransferRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferJob).where(
                    TransferJob.requested_by == requested_by,
                    TransferJob.idempotency_key == idempotency_key,
                )
            )
            if row is None:
                return None
            if row.idempotency_request_hash != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency key was already used for different input"
                )
            record = self._transfer(row)
            record.idempotent_replay = True
            return record

    async def ais_transfer_cancel_requested(self, transfer_id: uuid.UUID) -> bool:
        async with self.sessions() as session:
            requested_at = await session.scalar(
                select(TransferJob.cancel_requested_at).where(TransferJob.id == transfer_id)
            )
            return requested_at is not None

    async def adispatch_due_transfers(
        self,
        now: datetime,
        *,
        max_concurrent: int,
        max_nextcloud_concurrent: int = 1,
    ) -> DispatchBatch:
        """Reserve eligible transfers transactionally before sending them to Celery."""
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        if max_nextcloud_concurrent < 1:
            raise ValueError("max_nextcloud_concurrent must be positive")
        active_statuses = {"VALIDATING", "RUNNING", "RETRYING", "CANCELLING"}
        changed_ids: set[uuid.UUID] = set()
        admitted_ids: list[uuid.UUID] = []
        now = now.astimezone(UTC)

        async with self.sessions() as session:
            async with session.begin():
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": 0x43544D5343484544},
                    )

                stale_dispatches = await session.scalars(
                    select(TransferJob).where(
                        TransferJob.status.in_({"QUEUED", "SCHEDULED"}),
                        TransferJob.dispatch_token.is_not(None),
                        TransferJob.execution_started_at.is_(None),
                        TransferJob.dispatch_claimed_at < now - timedelta(seconds=120),
                    )
                )
                for stale in stale_dispatches:
                    stale.dispatch_token = None
                    stale.dispatch_claimed_at = None
                    changed_ids.add(stale.id)

                stale_running = list(
                    await session.scalars(
                        select(TransferJob).where(
                            TransferJob.status.in_({"QUEUED", *active_statuses}),
                            TransferJob.execution_started_at.is_not(None),
                            TransferJob.execution_heartbeat_at < now - timedelta(seconds=60),
                        )
                    )
                )
                for stale in stale_running:
                    worker_alive = False
                    if session.bind is not None and session.bind.dialect.name == "postgresql":
                        lock_key = self._transfer_lock_key(stale.id)
                        connection = await session.connection()
                        lock_available = bool(
                            await connection.scalar(
                                text("SELECT pg_try_advisory_lock(:lock_key)"),
                                {"lock_key": lock_key},
                            )
                        )
                        if lock_available:
                            await connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_key)"),
                                {"lock_key": lock_key},
                            )
                        else:
                            worker_alive = True
                    if worker_alive:
                        continue
                    stale.status = "FAILED"
                    stale.completed_at = now
                    stale.error_count += 1
                    stale.current_item = None
                    stale.wait_reason = None
                    stale.dispatch_token = None
                    stale.dispatch_claimed_at = None
                    stale.execution_started_at = None
                    stale.execution_heartbeat_at = None
                    await self._ensure_notification_outbox(session, stale)
                    session.add(
                        TransferEvent(
                            transfer_job_id=stale.id,
                            level="ERROR",
                            event_type="WORKER_INTERRUPTED",
                            message=(
                                "La ejecución se interrumpió; revisa el destino "
                                "antes de reintentar."
                            ),
                            technical_details={"reason": "STALE_WORKER_HEARTBEAT"},
                            provider_error_code="WORKER_INTERRUPTED",
                            correlation_id=stale.id,
                        )
                    )
                    changed_ids.add(stale.id)

                candidates = list(
                    await session.scalars(
                        select(TransferJob)
                        .where(
                            TransferJob.status.in_({"SCHEDULED", "QUEUED"}),
                            TransferJob.dispatch_token.is_(None),
                            or_(
                                TransferJob.scheduled_at.is_(None),
                                TransferJob.scheduled_at <= now,
                            ),
                        )
                        .order_by(
                            func.coalesce(TransferJob.scheduled_at, TransferJob.created_at),
                            TransferJob.created_at,
                            TransferJob.id,
                        )
                        .with_for_update()
                    )
                )
                parent_ids = {
                    transfer.depends_on_transfer_id
                    for transfer in candidates
                    if transfer.depends_on_transfer_id is not None
                }
                omitted_by_parent = (
                    {
                        transfer_id: int(count)
                        for transfer_id, count in (
                            await session.execute(
                                select(
                                    TransferManifestItem.transfer_job_id,
                                    func.count(),
                                )
                                .where(
                                    TransferManifestItem.transfer_job_id.in_(parent_ids),
                                    TransferManifestItem.is_dir.is_(False),
                                    TransferManifestItem.omission_kind.is_not(None),
                                )
                                .group_by(TransferManifestItem.transfer_job_id)
                            )
                        ).all()
                    }
                    if parent_ids
                    else {}
                )
                running = list(
                    await session.scalars(
                        select(TransferJob).where(
                            or_(
                                and_(
                                    TransferJob.dispatch_token.is_not(None),
                                    TransferJob.status.in_(ACTIVE_TRANSFER_STATUSES),
                                ),
                                TransferJob.status.in_(active_statuses),
                            )
                        )
                    )
                )
                connection_ids = {
                    connection_id
                    for transfer in [*candidates, *running]
                    for connection_id in (
                        transfer.source_connection_id,
                        transfer.destination_connection_id,
                    )
                }
                connection_rows = (
                    list(
                        await session.scalars(
                            select(StorageConnection).where(
                                StorageConnection.id.in_(connection_ids)
                            )
                        )
                    )
                    if connection_ids
                    else []
                )
                connections = {connection.id: connection for connection in connection_rows}

                active = [
                    TransferReservation(
                        transfer_id=transfer.id,
                        source_connection_id=transfer.source_connection_id,
                        destination_connection_id=transfer.destination_connection_id,
                        execution_mode=transfer.execution_mode,
                        uses_staging=self._transfer_uses_staging(transfer, connections),
                        uses_nextcloud=self._transfer_uses_nextcloud(transfer, connections),
                    )
                    for transfer in running
                ]
                exclusive_barrier = False
                reason: str | None = None

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
                        parent = await session.get(TransferJob, transfer.depends_on_transfer_id)
                        parent_wait = dependency_wait_reason(
                            parent.status if parent else "FAILED",
                            parent_omitted_count=omitted_by_parent.get(
                                transfer.depends_on_transfer_id, 0
                            ),
                            parent_attempt_count=parent.attempt_count if parent else 1,
                            accepted_parent_omissions_attempt=(
                                transfer.accepted_parent_omissions_attempt
                            ),
                        )
                        if parent_wait is not None:
                            evaluation = transfer.evaluation
                            if evaluation is not None and evaluation.status != "WAITING_PARENT":
                                restart_evaluation(evaluation, now=now)
                                evaluation.status = "WAITING_PARENT"
                                evaluation.safe_error_code = dependency_error_code(parent_wait)
                                evaluation.safe_message = dependency_wait_message(parent_wait)
                                evaluation.sequence = 1
                            elif evaluation is not None and (
                                evaluation.safe_error_code != dependency_error_code(parent_wait)
                                or evaluation.safe_message
                                != dependency_wait_message(parent_wait)
                            ):
                                evaluation.safe_error_code = dependency_error_code(parent_wait)
                                evaluation.safe_message = dependency_wait_message(parent_wait)
                                evaluation.sequence += 1
                                evaluation.updated_at = now
                            if transfer.wait_reason != parent_wait:
                                transfer.wait_reason = parent_wait
                                changed_ids.add(transfer.id)
                            if evaluation is not None:
                                changed_ids.add(transfer.id)
                            continue
                        if (
                            transfer.evaluation is not None
                            and transfer.evaluation.status == "WAITING_PARENT"
                        ):
                            restart_evaluation(transfer.evaluation, now=now)
                            transfer.wait_reason = "WAITING_EVALUATION"
                            changed_ids.add(transfer.id)
                    elif (
                        transfer.evaluation is not None
                        and transfer.evaluation.status == "WAITING_PARENT"
                    ):
                        restart_evaluation(transfer.evaluation, now=now)
                        transfer.wait_reason = "WAITING_EVALUATION"
                        changed_ids.add(transfer.id)

                    owner = await session.get(User, transfer.requested_by)
                    if owner is None or not owner.is_active:
                        reason = "WAITING_USER_ACTIVE"
                        if transfer.wait_reason != reason:
                            transfer.wait_reason = reason
                            changed_ids.add(transfer.id)
                        continue

                    source = connections.get(transfer.source_connection_id)
                    destination = connections.get(transfer.destination_connection_id)
                    if (
                        source is None
                        or destination is None
                        or source.deleted_at is not None
                        or destination.deleted_at is not None
                        or source.status != "ACTIVE"
                        or destination.status != "ACTIVE"
                    ):
                        reason = "WAITING_CONNECTION_ACTIVE"
                        if transfer.wait_reason != reason:
                            transfer.wait_reason = reason
                            changed_ids.add(transfer.id)
                        continue
                    if (
                        source.credential_version != transfer.source_credential_version
                        or destination.credential_version != transfer.destination_credential_version
                    ):
                        reason = "BLOCKED_CONNECTION_CHANGED"
                        if transfer.wait_reason != reason:
                            transfer.wait_reason = reason
                            changed_ids.add(transfer.id)
                        continue
                    if owner.role != "ADMIN":
                        grants = set(
                            (
                                await session.scalars(
                                    select(UserConnectionGrant.connection_id).where(
                                        UserConnectionGrant.user_id == owner.id,
                                        UserConnectionGrant.connection_id.in_(
                                            [source.id, destination.id]
                                        ),
                                    )
                                )
                            ).all()
                        )
                        if any(
                            not connection.is_global and connection.id not in grants
                            for connection in (source, destination)
                        ):
                            reason = "WAITING_CONNECTION_ACCESS"
                            if transfer.wait_reason != reason:
                                transfer.wait_reason = reason
                                changed_ids.add(transfer.id)
                            continue

                    evaluation = transfer.evaluation
                    if evaluation is not None:
                        maximum_files = (
                            self.settings.smb_transfer_max_files
                            if source.provider_type == "SMB"
                            else self.settings.transfer_max_files
                        )
                        if evaluation.max_files != maximum_files:
                            restart_evaluation(
                                evaluation,
                                now=now,
                                max_files=maximum_files,
                            )
                            transfer.wait_reason = "WAITING_EVALUATION"
                            changed_ids.add(transfer.id)
                            continue
                        evaluation_wait = evaluation_wait_reason(
                            EvaluationRecord.from_row(evaluation)
                        )
                        if evaluation_wait is not None:
                            if transfer.wait_reason != evaluation_wait:
                                transfer.wait_reason = evaluation_wait
                                changed_ids.add(transfer.id)
                            continue

                    if exclusive_barrier:
                        reason = "WAITING_EXCLUSIVE"
                    else:
                        reason = admission_wait_reason(
                            source_connection_id=transfer.source_connection_id,
                            destination_connection_id=transfer.destination_connection_id,
                            execution_mode=transfer.execution_mode,
                            uses_staging=self._transfer_uses_staging(transfer, connections),
                            uses_nextcloud=self._transfer_uses_nextcloud(transfer, connections),
                            active=active,
                            max_concurrent=max_concurrent,
                            max_nextcloud_concurrent=max_nextcloud_concurrent,
                        )

                    if reason is not None:
                        if transfer.wait_reason != reason:
                            transfer.wait_reason = reason
                            changed_ids.add(transfer.id)
                        if transfer.execution_mode == "EXCLUSIVE":
                            exclusive_barrier = True
                        continue

                    transfer.dispatch_token = uuid.uuid4()
                    transfer.dispatch_claimed_at = now
                    transfer.execution_started_at = None
                    transfer.execution_heartbeat_at = None
                    transfer.wait_reason = None
                    changed_ids.add(transfer.id)
                    admitted_ids.append(transfer.id)
                    active.append(
                        TransferReservation(
                            transfer_id=transfer.id,
                            source_connection_id=transfer.source_connection_id,
                            destination_connection_id=transfer.destination_connection_id,
                            execution_mode=transfer.execution_mode,
                            uses_staging=self._transfer_uses_staging(transfer, connections),
                            uses_nextcloud=self._transfer_uses_nextcloud(transfer, connections),
                        )
                    )
                    if transfer.execution_mode == "EXCLUSIVE":
                        exclusive_barrier = True

                rows = (
                    list(
                        await session.scalars(
                            select(TransferJob).where(
                                TransferJob.id.in_(changed_ids | set(admitted_ids))
                            )
                        )
                    )
                    if changed_ids or admitted_ids
                    else []
                )
                rows_by_id = {row.id: row for row in rows}
                admitted = [self._transfer(rows_by_id[item_id]) for item_id in admitted_ids]
                changed = [
                    self._transfer(rows_by_id[item_id])
                    for item_id in changed_ids
                    if item_id in rows_by_id
                ]

        return DispatchBatch(admitted=admitted, changed=changed)

    @staticmethod
    def _transfer_uses_staging(
        transfer: TransferJob, connections: dict[uuid.UUID, StorageConnection]
    ) -> bool:
        source = connections.get(transfer.source_connection_id)
        destination = connections.get(transfer.destination_connection_id)
        if source is None or destination is None:
            return False
        return transfer_requires_staging(
            source.provider_type,
            destination.provider_type,
            source.adapter_type,
            destination.adapter_type,
            transfer.conflict_policy,
        )

    @staticmethod
    def _transfer_uses_nextcloud(
        transfer: TransferJob, connections: dict[uuid.UUID, StorageConnection]
    ) -> bool:
        return any(
            (connection := connections.get(connection_id)) is not None
            and connection.provider_type == "NEXTCLOUD"
            for connection_id in (
                transfer.source_connection_id,
                transfer.destination_connection_id,
            )
        )

    async def arelease_transfer_dispatch(
        self, transfer_id: uuid.UUID, dispatch_token: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.dispatch_token == dispatch_token,
                        TransferJob.execution_started_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is not None:
                    row.dispatch_token = None
                    row.dispatch_claimed_at = None

    async def aupdate_transfer_schedule(
        self,
        transfer_id: uuid.UUID,
        *,
        scheduled_at: datetime | None,
        schedule_timezone: str,
        execution_mode: str,
        depends_on_transfer_id: uuid.UUID | None,
    ) -> TransferRecord | None:
        now = datetime.now(UTC)
        if scheduled_at is not None:
            scheduled_at = (
                scheduled_at.replace(tzinfo=UTC)
                if scheduled_at.tzinfo is None
                else scheduled_at.astimezone(UTC)
            )
        async with self.sessions() as session:
            async with session.begin():
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": 0x43544D5343484544},
                    )
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.status.in_({"SCHEDULED", "QUEUED"}),
                        TransferJob.dispatch_token.is_(None),
                        TransferJob.execution_started_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                dependency_changed = row.depends_on_transfer_id != depends_on_transfer_id
                if depends_on_transfer_id == transfer_id:
                    raise ValueError("Una transferencia no puede depender de sí misma.")
                seen: set[uuid.UUID] = set()
                ancestor_id = depends_on_transfer_id
                while ancestor_id is not None:
                    if ancestor_id == transfer_id or ancestor_id in seen:
                        raise ValueError("La dependencia crearía un ciclo.")
                    seen.add(ancestor_id)
                    ancestor = await session.get(TransferJob, ancestor_id)
                    if ancestor is None:
                        raise ValueError("La transferencia padre no existe.")
                    ancestor_id = ancestor.depends_on_transfer_id
                row.scheduled_at = scheduled_at
                row.schedule_timezone = schedule_timezone
                row.execution_mode = execution_mode
                row.depends_on_transfer_id = depends_on_transfer_id
                if dependency_changed:
                    row.accepted_parent_omissions_attempt = None
                due_later = scheduled_at is not None and scheduled_at > now
                row.status = "SCHEDULED" if due_later else "QUEUED"
                row.wait_reason = "WAITING_UNTIL" if due_later else None
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == transfer_id)
                    .with_for_update()
                )
                evaluation_created = False
                if evaluation is None and scheduled_at is not None:
                    source = await session.get(StorageConnection, row.source_connection_id)
                    maximum_files = (
                        self.settings.smb_transfer_max_files
                        if source is not None and source.provider_type == "SMB"
                        else self.settings.transfer_max_files
                    )
                    evaluation = TransferEvaluation(
                        id=uuid.uuid4(),
                        transfer_job_id=row.id,
                        status="PENDING",
                        generation=1,
                        sequence=0,
                        count_complete=False,
                        max_files=maximum_files,
                        source_credential_version=row.source_credential_version,
                        destination_credential_version=row.destination_credential_version,
                        attempt_count=0,
                        updated_at=now,
                    )
                    session.add(evaluation)
                    evaluation_created = True
                if evaluation is not None:
                    row.evaluation = evaluation

                if dependency_changed and evaluation is not None:
                    if not evaluation_created:
                        restart_evaluation(evaluation, now=now)
                    parent_wait = None
                    if depends_on_transfer_id is not None:
                        parent = await session.get(TransferJob, depends_on_transfer_id)
                        parent_omitted_count = int(
                            await session.scalar(
                                select(func.count()).where(
                                    TransferManifestItem.transfer_job_id == depends_on_transfer_id,
                                    TransferManifestItem.is_dir.is_(False),
                                    TransferManifestItem.omission_kind.is_not(None),
                                )
                            )
                            or 0
                        )
                        parent_wait = dependency_wait_reason(
                            parent.status if parent is not None else "FAILED",
                            parent_omitted_count=parent_omitted_count,
                            parent_attempt_count=parent.attempt_count if parent is not None else 1,
                            accepted_parent_omissions_attempt=(
                                row.accepted_parent_omissions_attempt
                            ),
                        )
                    if parent_wait is not None:
                        evaluation.status = "WAITING_PARENT"
                        evaluation.sequence = 1
                        evaluation.safe_error_code = dependency_error_code(parent_wait)
                        evaluation.safe_message = dependency_wait_message(parent_wait)
                        row.wait_reason = parent_wait
                    elif evaluation_created:
                        row.wait_reason = (
                            "WAITING_UNTIL" if due_later else "WAITING_EVALUATION"
                        )
                    else:
                        row.wait_reason = (
                            "WAITING_UNTIL" if due_later else "WAITING_EVALUATION"
                        )
                record = self._transfer(row, evaluation_override=evaluation)
                await session.flush()
                return record

    async def acancel_transfer(
        self, transfer_id: uuid.UUID, now: datetime
    ) -> tuple[TransferRecord | None, bool]:
        now = now.astimezone(UTC)
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer_id).with_for_update()
                )
                if row is None or row.status in {
                    "COMPLETED",
                    "COMPLETED_WITH_WARNINGS",
                    "FAILED",
                    "CANCELLED",
                }:
                    return None, False
                row.cancel_requested_at = now
                if row.status in {"QUEUED", "SCHEDULED"} and row.execution_started_at is None:
                    row.status = "CANCELLED"
                    row.completed_at = now
                    row.wait_reason = None
                    row.dispatch_token = None
                    row.dispatch_claimed_at = None
                    row.execution_heartbeat_at = None
                    await self._ensure_notification_outbox(session, row)
                    needs_worker_signal = False
                else:
                    row.status = "CANCELLING"
                    needs_worker_signal = True
                await session.flush()
                return self._transfer(row), needs_worker_signal

    async def aresume_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.status.in_({"FAILED", "CANCELLED"}),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                row.status = "QUEUED"
                row.attempt_count += 1
                row.completed_at = None
                row.current_item = None
                row.current_speed_bps = None
                row.estimated_remaining_seconds = None
                row.wait_reason = None
                row.dispatch_token = None
                row.dispatch_claimed_at = None
                row.execution_started_at = None
                row.execution_heartbeat_at = None
                row.cancel_requested_at = None
                await session.flush()
                return self._transfer(row)

    async def aaccept_parent_omissions(
        self,
        child_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        expected_parent_attempt: int,
        request_id: uuid.UUID,
    ) -> TransferRecord:
        request_hash = parent_omissions_request_hash(
            child_id=child_id,
            expected_parent_attempt=expected_parent_attempt,
        )
        async with self.sessions() as session:
            async with session.begin():
                child = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == child_id)
                    .with_for_update()
                )
                if child is None:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                actor = await session.get(User, actor_id)
                if actor is None or not actor.is_active:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if actor.role != "ADMIN" and child.requested_by != actor_id:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if child.depends_on_transfer_id is None:
                    raise TransferRecoveryConflictError("Child transfer has no parent")
                parent = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == child.depends_on_transfer_id)
                    .with_for_update()
                )
                if parent is None or (actor.role != "ADMIN" and parent.requested_by != actor_id):
                    raise TransferRecoveryNotFoundError("Parent transfer not found")
                prior_audits = (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.resource_type == "transfer",
                            AuditEvent.resource_id == str(child_id),
                            AuditEvent.action == "TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                        )
                    )
                ).all()
                for prior in prior_audits:
                    metadata = prior.event_metadata or {}
                    if metadata.get("request_id") != str(request_id):
                        continue
                    if metadata.get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different acceptance"
                        )
                    replay = self._transfer(child)
                    replay.idempotent_replay = True
                    return replay

                if child.status not in {"SCHEDULED", "QUEUED"}:
                    raise TransferRecoveryConflictError("Child transfer is not pending")
                if (
                    child.dispatch_token is not None
                    or child.dispatch_claimed_at is not None
                    or child.execution_started_at is not None
                    or child.execution_heartbeat_at is not None
                    or child.cancel_requested_at is not None
                ):
                    raise TransferRecoveryConflictError("Child transfer execution is still active")
                if expected_parent_attempt != parent.attempt_count:
                    raise TransferRecoveryConflictError("Parent transfer attempt changed")
                if parent.status not in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}:
                    raise TransferRecoveryConflictError(
                        "Only a successful terminal parent can be accepted"
                    )
                omitted_count = int(
                    await session.scalar(
                        select(func.count()).where(
                            TransferManifestItem.transfer_job_id == parent.id,
                            TransferManifestItem.is_dir.is_(False),
                            TransferManifestItem.omission_kind.is_not(None),
                        )
                    )
                    or 0
                )
                if omitted_count < 1:
                    raise TransferRecoveryConflictError("Parent has no explicit omissions")

                now = datetime.now(UTC)
                child.accepted_parent_omissions_attempt = expected_parent_attempt
                evaluation = await session.scalar(
                    select(TransferEvaluation)
                    .where(TransferEvaluation.transfer_job_id == child_id)
                    .with_for_update()
                )
                if evaluation is not None:
                    if evaluation.status == "WAITING_PARENT":
                        restart_evaluation(evaluation, now=now)
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
                    "omitted_count": omitted_count,
                }
                session.add(
                    AuditEvent(
                        actor_user_id=actor_id,
                        action="TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                        resource_type="transfer",
                        resource_id=str(child_id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(metadata),
                        correlation_id=request_id,
                    )
                )
                session.add(
                    TransferEvent(
                        transfer_job_id=child_id,
                        level="WARNING",
                        event_type="TRANSFER_PARENT_OMISSIONS_ACCEPTED",
                        message=(
                            "Se aceptaron las omisiones del intento vigente "
                            "de la transferencia padre."
                        ),
                        technical_details={
                            "parent_transfer_id": str(parent.id),
                            "parent_attempt": expected_parent_attempt,
                            "omitted_count": omitted_count,
                        },
                        correlation_id=child_id,
                    )
                )
                await session.flush()
                return self._transfer(child)

    async def aretry_omitted_transfer_items(
        self,
        transfer_id: uuid.UUID,
        *,
        item_ids: list[uuid.UUID],
        actor_id: uuid.UUID,
        expected_attempt: int,
        request_id: uuid.UUID,
    ) -> TransferRecord:
        if not 1 <= len(item_ids) <= 100 or len(item_ids) != len(set(item_ids)):
            raise TransferRecoveryConflictError("Select between 1 and 100 unique omitted files")
        request_hash = retry_omissions_request_hash(
            transfer_id=transfer_id,
            expected_attempt=expected_attempt,
            item_ids=item_ids,
        )
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == transfer_id)
                    .with_for_update()
                )
                if row is None:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                actor = await session.get(User, actor_id)
                if actor is None or not actor.is_active:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if actor.role != "ADMIN" and row.requested_by != actor_id:
                    raise TransferRecoveryNotFoundError("Transfer not found")

                prior_audits = (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.resource_type == "transfer",
                            AuditEvent.resource_id == str(transfer_id),
                            AuditEvent.action == "TRANSFER_OMITTED_FILES_RETRIED",
                        )
                    )
                ).all()
                for prior in prior_audits:
                    metadata = prior.event_metadata or {}
                    if metadata.get("request_id") != str(request_id):
                        continue
                    if metadata.get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different retry"
                        )
                    replay = self._transfer(row)
                    replay.idempotent_replay = True
                    return replay

                if expected_attempt != row.attempt_count:
                    raise TransferRecoveryConflictError("Transfer attempt changed")
                if row.status not in {"FAILED", "CANCELLED", "COMPLETED_WITH_WARNINGS"}:
                    raise TransferRecoveryConflictError(
                        "Transfer is not in a retryable terminal state"
                    )
                if (
                    row.dispatch_token is not None
                    or row.dispatch_claimed_at is not None
                    or row.execution_started_at is not None
                    or row.execution_heartbeat_at is not None
                ):
                    raise TransferRecoveryConflictError("Transfer execution is still active")

                connection_ids = sorted(
                    {row.source_connection_id, row.destination_connection_id}
                )
                connections = (
                    await session.scalars(
                        select(StorageConnection)
                        .where(StorageConnection.id.in_(connection_ids))
                        .order_by(StorageConnection.id)
                        .with_for_update()
                    )
                ).all()
                by_id = {connection.id: connection for connection in connections}
                source = by_id.get(row.source_connection_id)
                destination = by_id.get(row.destination_connection_id)
                if (
                    source is None
                    or destination is None
                    or source.deleted_at is not None
                    or destination.deleted_at is not None
                ):
                    raise TransferRecoveryConflictError("A transfer connection is unavailable")
                if source.status != "ACTIVE" or destination.status != "ACTIVE":
                    raise TransferRecoveryConflictError("A transfer connection is inactive")
                if (
                    source.credential_version != row.source_credential_version
                    or destination.credential_version != row.destination_credential_version
                ):
                    raise TransferRecoveryConflictError("Transfer credentials changed")
                if actor.role != "ADMIN":
                    for connection in (source, destination):
                        if not connection.is_global and await session.get(
                            UserConnectionGrant, (actor_id, connection.id)
                        ) is None:
                            raise TransferRecoveryNotFoundError("Transfer not found")

                selected = list(
                    await session.scalars(
                        select(TransferManifestItem)
                        .where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.id.in_(item_ids),
                        )
                        .order_by(TransferManifestItem.id)
                        .with_for_update()
                    )
                )
                if len(selected) != len(item_ids):
                    raise TransferRecoveryNotFoundError("Manifest item not found")
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

                old_attempt = row.attempt_count
                prior_omissions = [
                    {
                        "manifest_item_id": str(item.id),
                        "omission_kind": item.omission_kind,
                        "omitted_at": item.omitted_at.isoformat() if item.omitted_at else None,
                    }
                    for item in selected
                ]
                for item in selected:
                    item.status = "PENDING"
                    item.error_code = None
                    item.verified_by = None
                    item.omission_kind = None
                    item.omitted_at = None
                    item.omitted_by = None
                    item.last_failure_stage = None
                    item.last_failure_attempt = None

                row.status = "QUEUED"
                row.attempt_count += 1
                row.files_completed = max(0, row.files_completed - len(selected))
                row.warning_count = max(0, row.warning_count - len(selected))
                row.completed_at = None
                row.started_at = None
                row.current_item = None
                row.current_speed_bps = None
                row.estimated_remaining_seconds = None
                row.wait_reason = None
                row.dispatch_token = None
                row.dispatch_claimed_at = None
                row.execution_started_at = None
                row.execution_heartbeat_at = None
                row.cancel_requested_at = None
                row.source_file_error_policy = "STOP"

                count_rows = (
                    await session.execute(
                        select(TransferManifestItem.status, func.count())
                        .where(TransferManifestItem.transfer_job_id == transfer_id)
                        .group_by(TransferManifestItem.status)
                    )
                ).all()
                counts = {status: int(count) for status, count in count_rows}
                counts["pending"] = counts.get("PENDING", 0)
                counts["copied"] = counts.get("COPIED", 0)
                counts["failed"] = counts.get("FAILED", 0)
                counts["skipped"] = counts.get("SKIPPED", 0)
                row.progress_percent = smb_manifest_progress_percent(
                    bytes_transferred=row.bytes_transferred,
                    bytes_total=row.bytes_total or 0,
                    files_total=row.files_total or 0,
                    counts=counts,
                )
                metadata = {
                    "public_id": row.public_id,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "previous_attempt": old_attempt,
                    "attempt_count": row.attempt_count,
                    "retried_count": len(selected),
                    "items": prior_omissions,
                }
                session.add(
                    AuditEvent(
                        actor_user_id=actor_id,
                        action="TRANSFER_OMITTED_FILES_RETRIED",
                        resource_type="transfer",
                        resource_id=str(transfer_id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(metadata),
                        correlation_id=request_id,
                    )
                )
                session.add(
                    TransferEvent(
                        transfer_job_id=transfer_id,
                        level="WARNING",
                        event_type="TRANSFER_OMITTED_FILES_RETRIED",
                        message="Se reintentan los archivos omitidos seleccionados.",
                        technical_details=sanitize_data(
                            {
                                "previous_attempt": old_attempt,
                                "attempt_count": row.attempt_count,
                                "retried_count": len(selected),
                                "manifest_item_ids": [str(item.id) for item in selected],
                            }
                        ),
                        correlation_id=request_id,
                    )
                )
                await session.flush()
                return self._transfer(row)

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
        request_hash = recovery_request_hash(
            item_id=item_id,
            expected_attempt=expected_attempt,
            source_file_error_policy=source_file_error_policy,
        )
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(TransferJob.id == transfer_id)
                    .with_for_update()
                )
                if row is None:
                    raise TransferRecoveryNotFoundError("Transfer not found")

                prior_audits = (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.resource_type == "transfer",
                            AuditEvent.resource_id == str(transfer_id),
                            AuditEvent.action == "TRANSFER_FILE_OMITTED",
                        )
                    )
                ).all()
                for prior in prior_audits:
                    metadata = prior.event_metadata or {}
                    if metadata.get("request_id") != str(request_id):
                        continue
                    if metadata.get("request_hash") != request_hash:
                        raise TransferRecoveryConflictError(
                            "Request id was already used for a different recovery action"
                        )
                    replay = self._transfer(row)
                    replay.idempotent_replay = True
                    return replay

                actor = await session.get(User, actor_id)
                if actor is None or not actor.is_active:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if actor.role != "ADMIN" and row.requested_by != actor_id:
                    raise TransferRecoveryNotFoundError("Transfer not found")
                if expected_attempt != row.attempt_count:
                    raise TransferRecoveryConflictError("Transfer attempt changed")
                if row.status != "FAILED":
                    raise TransferRecoveryConflictError("Transfer is not failed")
                if (
                    row.dispatch_token is not None
                    or row.dispatch_claimed_at is not None
                    or row.execution_started_at is not None
                    or row.execution_heartbeat_at is not None
                ):
                    raise TransferRecoveryConflictError("Transfer execution is still active")

                connection_ids = sorted(
                    {row.source_connection_id, row.destination_connection_id}
                )
                connections = (
                    await session.scalars(
                        select(StorageConnection)
                        .where(StorageConnection.id.in_(connection_ids))
                        .order_by(StorageConnection.id)
                        .with_for_update()
                    )
                ).all()
                by_id = {connection.id: connection for connection in connections}
                source = by_id.get(row.source_connection_id)
                destination = by_id.get(row.destination_connection_id)
                if (
                    source is None
                    or destination is None
                    or source.deleted_at is not None
                    or destination.deleted_at is not None
                ):
                    raise TransferRecoveryConflictError("A transfer connection is unavailable")
                if source.status != "ACTIVE" or destination.status != "ACTIVE":
                    raise TransferRecoveryConflictError("A transfer connection is inactive")
                if (
                    source.credential_version != row.source_credential_version
                    or destination.credential_version != row.destination_credential_version
                ):
                    raise TransferRecoveryConflictError("Transfer credentials changed")
                if actor.role != "ADMIN":
                    for connection in (source, destination):
                        if not connection.is_global and await session.get(
                            UserConnectionGrant, (actor_id, connection.id)
                        ) is None:
                            raise TransferRecoveryNotFoundError("Transfer not found")

                item = await session.scalar(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.id == item_id,
                    )
                    .with_for_update()
                )
                if item is None:
                    raise TransferRecoveryNotFoundError("Manifest item not found")
                if source_file_error_policy is not None:
                    policy = normalize_source_file_error_policy(source_file_error_policy)
                    if policy != "STOP":
                        raise TransferRecoveryConflictError(
                            "Automatic source omission is not enabled"
                        )

                event_rows = (
                    await session.scalars(
                        select(TransferEvent)
                        .where(
                            TransferEvent.transfer_job_id == transfer_id,
                            TransferEvent.event_type == "FAILURE_DIAGNOSTIC",
                        )
                        .order_by(TransferEvent.created_at)
                    )
                ).all()
                events = [
                    {
                        "event_type": event.event_type,
                        "technical_details": event.technical_details,
                        "provider_error_code": event.provider_error_code,
                        "created_at": event.created_at,
                    }
                    for event in event_rows
                ]
                basename = PurePosixPath(item.source_path).name
                basename_matches = int(
                    await session.scalar(
                        select(func.count(TransferManifestItem.id)).where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.is_dir.is_(False),
                            or_(
                                TransferManifestItem.source_path == basename,
                                TransferManifestItem.source_path.endswith(
                                    f"/{basename}", autoescape=True
                                ),
                            ),
                        )
                    )
                    or 0
                )
                transfer = self._transfer(row)
                transfer.source_provider_type = source.provider_type
                failure_stage, failure_attempt = resolve_failure_evidence(
                    transfer,
                    self._manifest_item(item),
                    events,
                    basename_matches=basename_matches,
                )
                manifest_item = self._manifest_item(item)
                if not can_skip_manifest_item(
                    transfer,
                    manifest_item,
                    failure_stage=failure_stage,
                    failure_attempt=failure_attempt,
                ):
                    raise TransferRecoveryConflictError(
                        skip_unavailable_reason(
                            transfer,
                            manifest_item,
                            failure_stage=failure_stage,
                            failure_attempt=failure_attempt,
                        )
                        or "Transfer item cannot be omitted"
                    )

                now = datetime.now(UTC)
                previous_attempt = row.attempt_count
                item.status = "SKIPPED"
                item.omission_kind = OmissionKind.USER_ACCEPTED_SOURCE_FAILURE.value
                item.omitted_at = now
                item.omitted_by = actor_id
                item.last_failure_stage = failure_stage
                item.last_failure_attempt = failure_attempt
                row.error_count = max(0, row.error_count - 1)
                row.status = "QUEUED"
                row.attempt_count += 1
                row.completed_at = None
                row.started_at = None
                row.current_item = None
                row.current_speed_bps = None
                row.estimated_remaining_seconds = None
                row.wait_reason = None
                row.dispatch_token = None
                row.dispatch_claimed_at = None
                row.execution_started_at = None
                row.execution_heartbeat_at = None
                row.cancel_requested_at = None
                if source_file_error_policy is not None:
                    row.source_file_error_policy = normalize_source_file_error_policy(
                        source_file_error_policy
                    )

                metadata = {
                    "public_id": row.public_id,
                    "request_id": str(request_id),
                    "request_hash": request_hash,
                    "manifest_item_id": str(item.id),
                    "omission_kind": item.omission_kind,
                    "failure_attempt": failure_attempt,
                    "attempt_count": previous_attempt,
                }
                session.add(
                    AuditEvent(
                        actor_user_id=actor_id,
                        action="TRANSFER_FILE_OMITTED",
                        resource_type="transfer",
                        resource_id=str(transfer_id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(metadata),
                        correlation_id=request_id,
                    )
                )
                session.add(
                    AuditEvent(
                        actor_user_id=actor_id,
                        action="TRANSFER_RESUMED",
                        resource_type="transfer",
                        resource_id=str(transfer_id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(
                            {
                                "public_id": row.public_id,
                                "request_id": str(request_id),
                                "request_hash": request_hash,
                                "attempt_count": row.attempt_count,
                            }
                        ),
                        correlation_id=request_id,
                    )
                )
                session.add_all(
                    [
                        TransferEvent(
                            transfer_job_id=transfer_id,
                            level="WARNING",
                            event_type="TRANSFER_FILE_OMITTED",
                            message="Se omitió un archivo del origen por decisión del usuario.",
                            technical_details=sanitize_data(
                                {
                                    "manifest_item_id": str(item.id),
                                    "omission_kind": item.omission_kind,
                                    "failure_attempt": failure_attempt,
                                    "attempt_count": previous_attempt,
                                }
                            ),
                            provider_error_code=item.error_code,
                            item_path=item.source_path,
                            correlation_id=request_id,
                        ),
                        TransferEvent(
                            transfer_job_id=transfer_id,
                            level="INFO",
                            event_type="TRANSFER_RESUMED",
                            message="La transferencia continuará con los archivos restantes.",
                            technical_details={"attempt_count": row.attempt_count},
                            correlation_id=request_id,
                        ),
                    ]
                )
                await session.flush()
                return self._transfer(row)

    async def astart_transfer_dispatch(
        self, transfer_id: uuid.UUID, dispatch_token: uuid.UUID, now: datetime
    ) -> bool:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.dispatch_token == dispatch_token,
                        TransferJob.status.in_({"QUEUED", "CANCELLING"}),
                        TransferJob.execution_started_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                if row.status != "CANCELLING":
                    if row.depends_on_transfer_id is not None:
                        parent = await session.scalar(
                            select(TransferJob)
                            .where(TransferJob.id == row.depends_on_transfer_id)
                            .with_for_update()
                        )
                        parent_omitted_count = int(
                            await session.scalar(
                                select(func.count()).where(
                                    TransferManifestItem.transfer_job_id
                                    == row.depends_on_transfer_id,
                                    TransferManifestItem.is_dir.is_(False),
                                    TransferManifestItem.omission_kind.is_not(None),
                                )
                            )
                            or 0
                        )
                        parent_wait = dependency_wait_reason(
                            parent.status if parent is not None else "FAILED",
                            parent_omitted_count=parent_omitted_count,
                            parent_attempt_count=parent.attempt_count if parent is not None else 1,
                            accepted_parent_omissions_attempt=(
                                row.accepted_parent_omissions_attempt
                            ),
                        )
                        if parent_wait is not None:
                            row.status = "QUEUED"
                            row.wait_reason = parent_wait
                            row.dispatch_token = None
                            row.dispatch_claimed_at = None
                            evaluation = await session.scalar(
                                select(TransferEvaluation)
                                .where(TransferEvaluation.transfer_job_id == row.id)
                                .with_for_update()
                            )
                            if evaluation is not None:
                                evaluation.status = "WAITING_PARENT"
                                evaluation.safe_error_code = dependency_error_code(parent_wait)
                                evaluation.safe_message = dependency_wait_message(parent_wait)
                                evaluation.claim_token = None
                                evaluation.heartbeat_at = None
                                evaluation.sequence += 1
                                evaluation.updated_at = now.astimezone(UTC)
                            return False

                    owner = await session.get(User, row.requested_by)
                    source = await session.get(StorageConnection, row.source_connection_id)
                    destination = await session.get(
                        StorageConnection, row.destination_connection_id
                    )
                    wait_reason: str | None = None
                    if owner is None or not owner.is_active:
                        wait_reason = "WAITING_USER_ACTIVE"
                    elif (
                        source is None
                        or destination is None
                        or source.deleted_at is not None
                        or destination.deleted_at is not None
                        or source.status != "ACTIVE"
                        or destination.status != "ACTIVE"
                    ):
                        wait_reason = "WAITING_CONNECTION_ACTIVE"
                    elif (
                        source.credential_version != row.source_credential_version
                        or destination.credential_version != row.destination_credential_version
                    ):
                        wait_reason = "BLOCKED_CONNECTION_CHANGED"
                    elif owner.role != "ADMIN":
                        for connection in (source, destination):
                            if (
                                not connection.is_global
                                and await session.get(
                                    UserConnectionGrant, (owner.id, connection.id)
                                )
                                is None
                            ):
                                wait_reason = "WAITING_CONNECTION_ACCESS"
                                break
                    if wait_reason is not None:
                        row.status = "QUEUED"
                        row.wait_reason = wait_reason
                        row.dispatch_token = None
                        row.dispatch_claimed_at = None
                        return False
                row.execution_started_at = now.astimezone(UTC)
                row.execution_heartbeat_at = now.astimezone(UTC)
                return True

    async def aheartbeat_transfer_dispatch(
        self, transfer_id: uuid.UUID, dispatch_token: uuid.UUID, now: datetime
    ) -> bool:
        async with self.sessions() as session:
            updated_id = await session.scalar(
                update(TransferJob)
                .where(
                    TransferJob.id == transfer_id,
                    TransferJob.dispatch_token == dispatch_token,
                    TransferJob.execution_started_at.is_not(None),
                )
                .values(execution_heartbeat_at=now.astimezone(UTC))
                .returning(TransferJob.id)
            )
            await session.commit()
            return updated_id is not None

    async def afinish_transfer_dispatch(
        self, transfer_id: uuid.UUID, dispatch_token: uuid.UUID
    ) -> None:
        terminal_statuses = {
            "COMPLETED",
            "COMPLETED_WITH_WARNINGS",
            "FAILED",
            "CANCELLED",
        }
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.dispatch_token == dispatch_token,
                    )
                    .with_for_update()
                )
                if row is not None and row.status in terminal_statuses:
                    row.dispatch_token = None
                    row.dispatch_claimed_at = None
                    row.execution_started_at = None
                    row.execution_heartbeat_at = None

    async def afail_transfer_dispatch(
        self,
        transfer_id: uuid.UUID,
        dispatch_token: uuid.UUID,
        *,
        event_type: str,
        message: str,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob)
                    .where(
                        TransferJob.id == transfer_id,
                        TransferJob.dispatch_token == dispatch_token,
                    )
                    .with_for_update()
                )
                if row is None or row.status in {
                    "COMPLETED",
                    "COMPLETED_WITH_WARNINGS",
                    "FAILED",
                    "CANCELLED",
                }:
                    return
                row.status = "FAILED"
                row.completed_at = datetime.now(UTC)
                row.error_count += 1
                row.current_item = None
                row.wait_reason = None
                row.dispatch_token = None
                row.dispatch_claimed_at = None
                row.execution_started_at = None
                row.execution_heartbeat_at = None
                await self._ensure_notification_outbox(session, row)
                session.add(
                    TransferEvent(
                        transfer_job_id=row.id,
                        level="ERROR",
                        event_type=event_type,
                        message=message,
                        technical_details={"reason": event_type},
                        provider_error_code=event_type,
                        correlation_id=row.id,
                    )
                )

    async def alist_transfers(
        self,
        requested_by: uuid.UUID | None = None,
        pending_only: bool = False,
        background_only: bool = False,
        exclude_background: bool = False,
    ) -> list[TransferRecord]:
        criteria = self._transfer_list_filters(
            requested_by, pending_only, background_only, exclude_background
        )
        async with self.sessions() as session:
            statement = (
                select(TransferJob)
                .where(*criteria)
                .order_by(TransferJob.created_at.desc(), TransferJob.id.desc())
            )
            rows = (await session.scalars(statement)).all()
            return [self._transfer(row) for row in rows]

    async def alist_active_transfer_ids(self) -> set[uuid.UUID]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(TransferJob.id).where(TransferJob.status.in_(ACTIVE_TRANSFER_STATUSES))
            )
            return set(rows.all())

    @staticmethod
    def _transfer_list_filters(
        requested_by: uuid.UUID | None,
        pending_only: bool,
        background_only: bool,
        exclude_background: bool,
    ) -> list[Any]:
        criteria: list[Any] = []
        if requested_by is not None:
            criteria.append(TransferJob.requested_by == requested_by)
        if pending_only:
            criteria.append(TransferJob.status.in_({"SCHEDULED", "QUEUED"}))
        programmed = or_(
            TransferJob.scheduled_at.is_not(None),
            TransferJob.wait_reason.is_not(None),
            TransferJob.execution_mode == "EXCLUSIVE",
            TransferJob.depends_on_transfer_id.is_not(None),
        )
        background = and_(programmed, TransferJob.status.in_(ACTIVE_TRANSFER_STATUSES))
        if background_only:
            criteria.append(background)
        if exclude_background:
            criteria.append(~background)
        return criteria

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
        criteria = self._transfer_list_filters(
            requested_by, pending_only, background_only, exclude_background
        )
        async with self.sessions() as session:
            total = int(
                await session.scalar(select(func.count(TransferJob.id)).where(*criteria)) or 0
            )
            statement = (
                select(TransferJob)
                .where(*criteria)
                .order_by(TransferJob.created_at.desc(), TransferJob.id.desc())
                .offset(max(0, offset))
                .limit(max(1, min(limit, 200)))
            )
            rows = (await session.scalars(statement)).all()
            return [self._transfer(row) for row in rows], total

    async def asave_transfer(self, transfer: TransferRecord) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer.id).with_for_update()
                )
                if row is None or row.status in _TERMINAL_TRANSFER_STATUSES:
                    return
                if transfer.attempt_count < row.attempt_count:
                    return
                if _TRANSFER_STATUS_RANK.get(transfer.status, -1) < _TRANSFER_STATUS_RANK.get(
                    row.status, -1
                ):
                    return
                row.status = transfer.status
                row.progress_percent = transfer.progress_percent
                row.files_total = transfer.files_total
                row.files_completed = transfer.files_completed
                row.bytes_total = transfer.bytes_total
                row.bytes_transferred = transfer.bytes_transferred
                row.current_item = transfer.current_item
                row.average_speed_bps = transfer.average_speed_bps
                row.current_speed_bps = transfer.current_speed_bps
                row.estimated_remaining_seconds = transfer.estimated_remaining_seconds
                row.attempt_count = transfer.attempt_count
                row.error_count = transfer.error_count
                row.warning_count = transfer.warning_count
                if transfer.started_at is not None:
                    row.started_at = transfer.started_at
                if transfer.completed_at is not None:
                    row.completed_at = transfer.completed_at
                await self._ensure_notification_outbox(session, row)

    async def aadd_transfer_manifest_items(
        self, transfer_id: uuid.UUID, entries: list[dict[str, Any]]
    ) -> int:
        if not entries:
            return 0
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            inserted = 0
            async with session.begin():
                for start in range(0, len(entries), 200):
                    values = []
                    for entry in entries[start : start + 200]:
                        source_path = str(entry["source_path"])
                        source_object_id = entry.get("source_object_id")
                        identity = (
                            f"{transfer_id}:object:{source_object_id}"
                            if isinstance(source_object_id, str) and source_object_id
                            else f"{transfer_id}:path:{source_path}"
                        )
                        is_dir = bool(entry.get("is_dir", False))
                        values.append(
                            {
                                "id": uuid.uuid4(),
                                "transfer_job_id": transfer_id,
                                "source_path": source_path,
                                "source_object_id": (
                                    source_object_id if isinstance(source_object_id, str) else None
                                ),
                                "destination_path": str(entry["destination_path"]),
                                "requested_destination_path": str(
                                    entry.get(
                                        "requested_destination_path", entry["destination_path"]
                                    )
                                ),
                                "rename_reason": (
                                    str(entry["rename_reason"])
                                    if entry.get("rename_reason")
                                    else None
                                ),
                                "source_path_digest": hashlib.sha256(identity.encode()).hexdigest(),
                                "is_dir": is_dir,
                                "size_bytes": entry.get("size_bytes"),
                                "modified_at": entry.get("modified_at"),
                                "scan_complete": False,
                                "status": "DIRECTORY" if is_dir else "PENDING",
                                "copy_started": False,
                                "destination_existed_before": False,
                            }
                        )
                    statement = insert(TransferManifestItem).values(values)
                    result = await session.execute(
                        statement.on_conflict_do_nothing(
                            index_elements=[
                                TransferManifestItem.transfer_job_id,
                                TransferManifestItem.source_path_digest,
                            ]
                        ).returning(TransferManifestItem.id)
                    )
                    inserted += len(result.scalars().all())
                transfer = await session.get(TransferJob, transfer_id)
                if transfer is None:
                    raise ValueError("Transfer not found")
                transfer.files_total = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TransferManifestItem)
                        .where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.is_dir.is_(False),
                        )
                    )
                    or 0
                )
                transfer.bytes_total = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(TransferManifestItem.size_bytes), 0)).where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.is_dir.is_(False),
                        )
                    )
                    or 0
                )
            return inserted

    async def alist_pending_scan_directories(
        self, transfer_id: uuid.UUID, limit: int = 200
    ) -> list[TransferManifestItemRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.is_dir.is_(True),
                        TransferManifestItem.scan_complete.is_(False),
                    )
                    .order_by(TransferManifestItem.id)
                    .limit(max(1, min(limit, 1000)))
                )
            ).all()
            return [self._manifest_item(row) for row in rows]

    async def amark_transfer_directory_scanned(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == transfer_id,
                    TransferManifestItem.id == item_id,
                    TransferManifestItem.is_dir.is_(True),
                )
            )
            if row is None:
                raise ValueError("Manifest directory not found")
            row.scan_complete = True
            await session.commit()

    async def amark_transfer_manifest_item_started(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        destination_existed_before: bool,
    ) -> None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferManifestItem)
                .where(
                    TransferManifestItem.transfer_job_id == transfer_id,
                    TransferManifestItem.id == item_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Manifest file not found")
            if not row.copy_started:
                row.copy_started = True
                row.destination_existed_before = destination_existed_before
            await session.commit()

    async def aupdate_transfer_manifest_item_size(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID, *, size_bytes: int
    ) -> None:
        if size_bytes < 0:
            raise ValueError("Manifest file size cannot be negative")
        async with self.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer_id).with_for_update()
                )
                item = await session.scalar(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.id == item_id,
                        TransferManifestItem.is_dir.is_(False),
                    )
                    .with_for_update()
                )
                if transfer is None or item is None or item.copy_started:
                    raise ValueError("Manifest file size cannot be updated")
                if item.size_bytes is not None and item.size_bytes != size_bytes:
                    raise ValueError("Manifest file size is already fixed")
                if item.size_bytes is None:
                    item.size_bytes = size_bytes
                    transfer.bytes_total = int(transfer.bytes_total or 0) + size_bytes

    async def aupdate_transfer_manifest_destination(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        destination_path: str,
        requested_destination_path: str,
        rename_reason: str | None,
    ) -> None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferManifestItem)
                .where(
                    TransferManifestItem.transfer_job_id == transfer_id,
                    TransferManifestItem.id == item_id,
                    TransferManifestItem.is_dir.is_(False),
                )
                .with_for_update()
            )
            if row is None or row.copy_started:
                raise ValueError("Manifest destination can only change before a copy starts")
            row.destination_path = destination_path
            row.requested_destination_path = requested_destination_path
            row.rename_reason = rename_reason
            await session.commit()

    async def alist_transfer_manifest_items(
        self,
        transfer_id: uuid.UUID,
        statuses: set[str] | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 200,
        include_directories: bool = False,
    ) -> list[TransferManifestItemRecord]:
        async with self.sessions() as session:
            statement = select(TransferManifestItem).where(
                TransferManifestItem.transfer_job_id == transfer_id,
            )
            if not include_directories:
                statement = statement.where(TransferManifestItem.is_dir.is_(False))
            if statuses:
                statement = statement.where(TransferManifestItem.status.in_(statuses))
            if after_id is not None:
                statement = statement.where(TransferManifestItem.id > after_id)
            rows = (
                await session.scalars(
                    statement.order_by(TransferManifestItem.id).limit(max(1, min(limit, 1000)))
                )
            ).all()
            return [self._manifest_item(row) for row in rows]

    async def acount_transfer_manifest_basename_matches(
        self, transfer_id: uuid.UUID, basename: str
    ) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count(TransferManifestItem.id)).where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.is_dir.is_(False),
                        or_(
                            TransferManifestItem.source_path == basename,
                            TransferManifestItem.source_path.endswith(
                                f"/{basename}", autoescape=True
                            ),
                        ),
                    )
                )
                or 0
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
        async with self.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer_id).with_for_update()
                )
                item = await session.scalar(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.id == item_id,
                        TransferManifestItem.is_dir.is_(False),
                    )
                    .with_for_update()
                )
                if transfer is None or item is None:
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
                transfer.progress_percent = smb_manifest_progress_percent(
                    bytes_transferred=transfer.bytes_transferred,
                    bytes_total=transfer.bytes_total or 0,
                    files_total=transfer.files_total or 0,
                    counts={
                        "copied": max(0, transfer.files_completed - transfer.warning_count),
                        "failed": transfer.error_count,
                        "skipped": max(0, transfer.warning_count - transfer.error_count),
                        "pending": max(0, (transfer.files_total or 0) - transfer.files_completed),
                    },
                )

    async def aget_transfer_manifest_counts(self, transfer_id: uuid.UUID) -> dict[str, int]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(TransferManifestItem.status, func.count())
                    .where(TransferManifestItem.transfer_job_id == transfer_id)
                    .group_by(TransferManifestItem.status)
                )
            ).all()
            counts = {status: int(count) for status, count in rows}
            pending_directories = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.is_dir.is_(True),
                        TransferManifestItem.scan_complete.is_(False),
                    )
                )
                or 0
            )
            return {
                "total": sum(counts.values()),
                "pending": counts.get("PENDING", 0) + pending_directories,
                "copied": counts.get("COPIED", 0),
                "skipped": counts.get("SKIPPED", 0),
                "failed": counts.get("FAILED", 0),
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
        if not transfer_ids:
            return summaries

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        TransferManifestItem.transfer_job_id,
                        func.sum(case((TransferManifestItem.status == "COPIED", 1), else_=0)).label(
                            "copied"
                        ),
                        func.sum(case((TransferManifestItem.status == "FAILED", 1), else_=0)).label(
                            "failed"
                        ),
                        func.sum(
                            case((TransferManifestItem.status == "SKIPPED", 1), else_=0)
                        ).label("skipped"),
                        func.sum(
                            case((TransferManifestItem.status == "PENDING", 1), else_=0)
                        ).label("pending"),
                        func.sum(
                            case((TransferManifestItem.rename_reason.is_not(None), 1), else_=0)
                        ).label("renamed"),
                        func.sum(
                            case((TransferManifestItem.omission_kind.is_not(None), 1), else_=0)
                        ).label("omitted"),
                        func.sum(
                            case(
                                (
                                    TransferManifestItem.omission_kind.is_not(None),
                                    TransferManifestItem.size_bytes,
                                ),
                                else_=0,
                            )
                        ).label("omitted_bytes"),
                    )
                    .where(
                        TransferManifestItem.transfer_job_id.in_(transfer_ids),
                    )
                    .group_by(TransferManifestItem.transfer_job_id)
                )
            ).all()
        for transfer_id, copied, failed, skipped, pending, renamed, omitted, omitted_bytes in rows:
            summaries[transfer_id] = {
                "copied": int(copied or 0),
                "failed": int(failed or 0),
                "skipped": int(skipped or 0),
                "pending": int(pending or 0),
                "renamed": int(renamed or 0),
                "omitted": int(omitted or 0),
                "omitted_bytes": int(omitted_bytes or 0),
            }
        return summaries

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
        async with self.sessions() as session:
            session.add(
                TransferEvent(
                    transfer_job_id=transfer_job_id,
                    level=level,
                    event_type=event_type[:64],
                    message=message[:2000],
                    technical_details=sanitize_data(technical_details),
                    provider_error_code=provider_error_code,
                    item_path=item_path[:1024] if item_path else None,
                    correlation_id=correlation_id or transfer_job_id,
                )
            )
            await session.commit()

    async def alist_transfer_events(self, transfer_job_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(TransferEvent)
                    .where(TransferEvent.transfer_job_id == transfer_job_id)
                    .order_by(TransferEvent.created_at)
                )
            ).all()
            return [
                {
                    "id": str(row.id),
                    "level": row.level,
                    "event_type": row.event_type,
                    "message": row.message,
                    "technical_details": row.technical_details,
                    "provider_error_code": row.provider_error_code,
                    "item_path": row.item_path,
                    "correlation_id": str(row.correlation_id),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

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
        async with self.sessions() as session:
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    action=action[:64],
                    resource_type=resource_type[:64],
                    resource_id=resource_id[:64] if resource_id else None,
                    result=result,
                    event_metadata=sanitize_data(metadata),
                    ip_address=ip_address,
                    user_agent=user_agent[:255] if user_agent else None,
                )
            )
            await session.commit()

    async def alist_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
                )
            ).all()
            return [
                {
                    "id": str(row.id),
                    "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "result": row.result,
                    "metadata": row.event_metadata,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
