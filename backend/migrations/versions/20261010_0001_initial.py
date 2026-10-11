"""Create the initial Cloud Transfer Manager schema.

Revision ID: 20261010_0001
Revises:
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261010_0001"
down_revision = None
branch_labels = None
depends_on = None

JSON_VALUE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("role IN ('ADMIN', 'USER')", name="ck_users_role"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "storage_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("root_path", sa.String(length=512), nullable=True),
        sa.Column("is_global", sa.Boolean(), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_status", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE')", name="ck_storage_provider_type"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'ERROR')", name="ck_connection_status"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_connection_grants",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["storage_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "connection_id"),
    )

    op.create_table(
        "transfer_id_counters",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("year"),
    )

    op.create_table(
        "transfer_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("source_connection_id", sa.Uuid(), nullable=False),
        sa.Column("destination_connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_paths", JSON_VALUE, nullable=False),
        sa.Column("destination_path", sa.String(length=1024), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("conflict_policy", sa.String(length=32), nullable=False),
        sa.Column("send_email_notification", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("files_total", sa.BigInteger(), nullable=True),
        sa.Column("files_completed", sa.BigInteger(), nullable=False),
        sa.Column("bytes_total", sa.BigInteger(), nullable=True),
        sa.Column("bytes_transferred", sa.BigInteger(), nullable=False),
        sa.Column("current_item", sa.String(length=1024), nullable=True),
        sa.Column("average_speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("current_speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("estimated_remaining_seconds", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("operation = 'COPY'", name="ck_transfer_copy_only"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'VALIDATING', 'RUNNING', 'CANCELLING', 'CANCELLED', "
            "'COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'RETRYING')",
            name="ck_transfer_status",
        ),
        sa.ForeignKeyConstraint(["destination_connection_id"], ["storage_connections.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_connection_id"], ["storage_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_transfer_jobs_status", "transfer_jobs", ["status"], unique=False)

    op.create_table(
        "transfer_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_job_id", sa.Uuid(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("technical_details", JSON_VALUE, nullable=True),
        sa.Column("provider_error_code", sa.String(length=64), nullable=True),
        sa.Column("item_path", sa.String(length=1024), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')", name="ck_transfer_event_level"
        ),
        sa.ForeignKeyConstraint(["transfer_job_id"], ["transfer_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("metadata", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("result IN ('SUCCESS', 'FAILURE')", name="ck_audit_result"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"], unique=False)
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION prevent_audit_event_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'audit_events are immutable'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS prevent_audit_event_mutation()")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("transfer_events")
    op.drop_index("ix_transfer_jobs_status", table_name="transfer_jobs")
    op.drop_table("transfer_jobs")
    op.drop_table("transfer_id_counters")
    op.drop_table("user_connection_grants")
    op.drop_table("storage_connections")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
