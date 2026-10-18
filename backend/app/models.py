import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('ADMIN', 'USER')", name="ck_users_role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="USER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class StorageConnection(Base):
    __tablename__ = "storage_connections"
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB', 'NEXTCLOUD')",
            name="ck_storage_provider_type",
        ),
        CheckConstraint(
            "adapter_type IN ('RCLONE', 'SEAFILE_API')", name="ck_storage_adapter_type"
        ),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE', 'ERROR')", name="ck_connection_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    provider_type: Mapped[str] = mapped_column(String(32))
    adapter_type: Mapped[str] = mapped_column(String(32), default="RCLONE", server_default="RCLONE")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    root_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_global: Mapped[bool] = mapped_column(Boolean, default=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))


class SMTPSettings(Base):
    __tablename__ = "smtp_settings"
    __table_args__ = (CheckConstraint("scope = 'global'", name="ck_smtp_settings_global_scope"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(
        String(16), unique=True, default="global", server_default="global"
    )
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    use_tls: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    from_address: Mapped[str] = mapped_column(String(255))
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary)


class UserConnectionGrant(Base):
    __tablename__ = "user_connection_grants"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("storage_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransferJob(Base):
    __tablename__ = "transfer_jobs"
    __table_args__ = (
        CheckConstraint("operation = 'COPY'", name="ck_transfer_copy_only"),
        CheckConstraint(
            "status IN ('SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'PAUSING', 'PAUSED', "
            "'CANCELLING', 'CANCELLED', 'COMPLETED', 'COMPLETED_WITH_WARNINGS', "
            "'FAILED', 'RETRYING')",
            name="ck_transfer_status",
        ),
        CheckConstraint(
            "execution_mode IN ('AUTO', 'EXCLUSIVE')", name="ck_transfer_execution_mode"
        ),
        CheckConstraint(
            "source_file_error_policy IN ('STOP', 'SKIP_CONFIRMED_RESTRICTED')",
            name="ck_transfer_source_file_error_policy",
        ),
        CheckConstraint(
            "accepted_parent_omissions_attempt IS NULL OR accepted_parent_omissions_attempt >= 1",
            name="ck_transfer_accepted_parent_omissions_attempt",
        ),
        Index("ix_transfer_dispatch", "status", "scheduled_at", "created_at"),
        UniqueConstraint("requested_by", "idempotency_key", name="uq_transfer_request_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    idempotency_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_connections.id")
    )
    destination_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_connections.id")
    )
    source_credential_version: Mapped[int] = mapped_column(Integer, default=1)
    destination_credential_version: Mapped[int] = mapped_column(Integer, default=1)
    source_paths: Mapped[list[str]] = mapped_column(JSON_VALUE)
    source_item_ids: Mapped[list[str | None] | None] = mapped_column(JSON_VALUE, nullable=True)
    source_parent_ids: Mapped[list[str | None] | None] = mapped_column(JSON_VALUE, nullable=True)
    destination_path: Mapped[str] = mapped_column(String(1024))
    operation: Mapped[str] = mapped_column(String(16), default="COPY")
    conflict_policy: Mapped[str] = mapped_column(String(32), default="KEEP_BOTH")
    send_email_notification: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    execution_mode: Mapped[str] = mapped_column(String(16), default="AUTO", server_default="AUTO")
    depends_on_transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transfer_jobs.id", ondelete="RESTRICT"), nullable=True
    )
    source_file_error_policy: Mapped[str] = mapped_column(
        String(32), default="STOP", server_default="STOP"
    )
    accepted_parent_omissions_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wait_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dispatch_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pause_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    progress_percent: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    files_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    files_completed: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    current_item: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    average_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_remaining_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    evaluation: Mapped["TransferEvaluation | None"] = relationship(
        back_populates="transfer", uselist=False, lazy="selectin"
    )


class TransferEvaluation(Base):
    __tablename__ = "transfer_evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'EVALUATING', 'APPROVED', 'BLOCKED', 'ERROR', 'WAITING_PARENT')",
            name="ck_transfer_evaluation_status",
        ),
        CheckConstraint("generation >= 1", name="ck_transfer_evaluation_generation"),
        CheckConstraint("sequence >= 0", name="ck_transfer_evaluation_sequence"),
        CheckConstraint(
            "files_found IS NULL OR files_found >= 0", name="ck_evaluation_files_found"
        ),
        CheckConstraint("max_files IS NULL OR max_files >= 1", name="ck_evaluation_max_files"),
        CheckConstraint("attempt_count >= 0", name="ck_evaluation_attempt_count"),
        Index("ix_transfer_evaluation_dispatch", "status", "retry_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("transfer_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(24), default="PENDING", server_default="PENDING")
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_found: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    count_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    max_files: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_credential_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_credential_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transfer: Mapped[TransferJob] = relationship(back_populates="evaluation")


class TransferManifestItem(Base):
    __tablename__ = "transfer_manifest_items"
    __table_args__ = (
        UniqueConstraint(
            "transfer_job_id", "source_path_digest", name="uq_transfer_manifest_source"
        ),
        CheckConstraint(
            "status IN ('DIRECTORY', 'PENDING', 'COPIED', 'SKIPPED', 'FAILED')",
            name="ck_transfer_manifest_status",
        ),
        CheckConstraint(
            "omission_kind IS NULL OR omission_kind IN "
            "('USER_ACCEPTED_SOURCE_FAILURE', 'MEGA_FILE_RESTRICTED')",
            name="ck_transfer_manifest_omission_kind",
        ),
        CheckConstraint(
            "omission_kind IS NULL OR (is_dir = false AND status = 'SKIPPED' "
            "AND omitted_at IS NOT NULL AND copy_started = false)",
            name="ck_transfer_manifest_explicit_omission",
        ),
        CheckConstraint(
            "omission_kind IS NOT NULL OR (omitted_at IS NULL AND omitted_by IS NULL)",
            name="ck_transfer_manifest_omission_metadata",
        ),
        CheckConstraint(
            "last_failure_attempt IS NULL OR last_failure_attempt >= 1",
            name="ck_transfer_manifest_last_failure_attempt",
        ),
        Index("ix_transfer_manifest_scan", "transfer_job_id", "is_dir", "scan_complete"),
        Index("ix_transfer_manifest_status", "transfer_job_id", "status", "id"),
        Index(
            "ix_transfer_incident_failed",
            "updated_at",
            "id",
            postgresql_where=text("status = 'FAILED' AND is_dir = false"),
            sqlite_where=text("status = 'FAILED' AND is_dir = 0"),
        ),
        Index(
            "ix_transfer_incident_omitted",
            "omitted_at",
            "id",
            postgresql_where=text("omission_kind IS NOT NULL AND is_dir = false"),
            sqlite_where=text("omission_kind IS NOT NULL AND is_dir = 0"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transfer_jobs.id", ondelete="CASCADE")
    )
    source_path: Mapped[str] = mapped_column(String(2048))
    source_object_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    destination_path: Mapped[str] = mapped_column(String(2048))
    requested_destination_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rename_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_path_digest: Mapped[str] = mapped_column(String(64))
    is_dir: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    copy_started: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_existed_before: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    omission_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    omitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    omitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_transfer_manifest_items_omitted_by_users",
        ),
        nullable=True,
    )
    last_failure_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_failure_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransferCounter(Base):
    __tablename__ = "transfer_id_counters"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class TransferEvent(Base):
    __tablename__ = "transfer_events"
    __table_args__ = (
        CheckConstraint(
            "level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')", name="ck_transfer_event_level"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transfer_jobs.id", ondelete="CASCADE")
    )
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    technical_details: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransferNotificationOutbox(Base):
    __tablename__ = "transfer_notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "transfer_job_id", "transfer_attempt", name="uq_transfer_notification_attempt"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'SENDING', 'SENT', 'FAILED')",
            name="ck_transfer_notification_status",
        ),
        Index("ix_notification_dispatch", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transfer_jobs.id", ondelete="CASCADE")
    )
    transfer_attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (CheckConstraint("result IN ('SUCCESS', 'FAILURE')", name="ck_audit_result"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16))
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, default=dict, server_default="{}"
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
