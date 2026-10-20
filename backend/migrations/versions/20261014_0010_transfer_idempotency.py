"""Add idempotency keys to transfer creation.

Revision ID: 20261014_0010
Revises: 20261013_0009
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "20261014_0010"
down_revision = "20261013_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("idempotency_request_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_transfer_request_idempotency", ["requested_by", "idempotency_key"]
        )


def downgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_constraint("uq_transfer_request_idempotency", type_="unique")
        batch_op.drop_column("idempotency_request_hash")
        batch_op.drop_column("idempotency_key")
