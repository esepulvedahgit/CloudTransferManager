"""Persist terminal transfer email notifications in an outbox.

Revision ID: 20261014_0011
Revises: 20261014_0010
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261014_0011"
down_revision = "20261014_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transfer_notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_job_id", sa.Uuid(), nullable=False),
        sa.Column("transfer_attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'SENDING', 'SENT', 'FAILED')",
            name="ck_transfer_notification_status",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_job_id"], ["transfer_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transfer_job_id", "transfer_attempt", name="uq_transfer_notification_attempt"
        ),
    )
    op.create_index(
        "ix_notification_dispatch",
        "transfer_notification_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_dispatch", table_name="transfer_notification_outbox")
    op.drop_table("transfer_notification_outbox")
