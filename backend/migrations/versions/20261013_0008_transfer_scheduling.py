"""Add one-time transfer scheduling and durable dispatch reservations.

Revision ID: 20261013_0008
Revises: 20261012_0007
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20261013_0008"
down_revision = "20261012_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_constraint("ck_transfer_status", type_="check")
        batch_op.add_column(sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "schedule_timezone",
                sa.String(length=64),
                server_default="UTC",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("execution_mode", sa.String(length=16), server_default="AUTO", nullable=False)
        )
        batch_op.add_column(sa.Column("depends_on_transfer_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("wait_reason", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("dispatch_token", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("dispatch_claimed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("execution_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_transfer_jobs_dependency",
            "transfer_jobs",
            ["depends_on_transfer_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_transfer_status",
            "status IN ('SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'CANCELLING', "
            "'CANCELLED', 'COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'RETRYING')",
        )
        batch_op.create_check_constraint(
            "ck_transfer_execution_mode", "execution_mode IN ('AUTO', 'EXCLUSIVE')"
        )
        batch_op.create_index(
            "ix_transfer_dispatch", ["status", "scheduled_at", "created_at"]
        )
    op.execute(
        sa.text(
            "UPDATE transfer_jobs SET execution_started_at = started_at, "
            "execution_heartbeat_at = started_at "
            "WHERE status IN ('VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING') "
            "AND started_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    # Preserve pending work in the previous schema as ordinary queued transfers.
    op.execute(
        sa.text("UPDATE transfer_jobs SET status = 'QUEUED' WHERE status = 'SCHEDULED'")
    )
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_index("ix_transfer_dispatch")
        batch_op.drop_constraint("ck_transfer_execution_mode", type_="check")
        batch_op.drop_constraint("ck_transfer_status", type_="check")
        batch_op.create_check_constraint(
            "ck_transfer_status",
            "status IN ('QUEUED', 'VALIDATING', 'RUNNING', 'CANCELLING', 'CANCELLED', "
            "'COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'RETRYING')",
        )
        batch_op.drop_constraint("fk_transfer_jobs_dependency", type_="foreignkey")
        batch_op.drop_column("execution_heartbeat_at")
        batch_op.drop_column("execution_started_at")
        batch_op.drop_column("dispatch_claimed_at")
        batch_op.drop_column("dispatch_token")
        batch_op.drop_column("wait_reason")
        batch_op.drop_column("depends_on_transfer_id")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("schedule_timezone")
        batch_op.drop_column("scheduled_at")
