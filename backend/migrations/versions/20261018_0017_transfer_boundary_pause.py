"""Persist transfer pause requests at file boundaries.

Revision ID: 20261018_0017
Revises: 20261018_0016
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0017"
down_revision = "20261018_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_constraint("ck_transfer_status", type_="check")
        batch_op.create_check_constraint(
            "ck_transfer_status",
            "status IN ('SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'PAUSING', 'PAUSED', "
            "'CANCELLING', 'CANCELLED', 'COMPLETED', 'COMPLETED_WITH_WARNINGS', "
            "'FAILED', 'RETRYING')",
        )
        batch_op.add_column(
            sa.Column("pause_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    paused = (
        op.get_bind()
        .execute(
            sa.text("SELECT COUNT(*) FROM transfer_jobs WHERE status IN ('PAUSING', 'PAUSED')")
        )
        .scalar_one()
    )
    if paused:
        raise RuntimeError(
            "Cannot downgrade while transfers are PAUSING or PAUSED; resume them first."
        )

    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_column("state_version")
        batch_op.drop_column("paused_at")
        batch_op.drop_column("pause_requested_at")
        batch_op.drop_constraint("ck_transfer_status", type_="check")
        batch_op.create_check_constraint(
            "ck_transfer_status",
            "status IN ('SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'CANCELLING', "
            "'CANCELLED', 'COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'RETRYING')",
        )
