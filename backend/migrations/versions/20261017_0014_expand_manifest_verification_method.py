"""Allow descriptive manifest verification methods.

Revision ID: 20261017_0014
Revises: 20261017_0013
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "20261017_0014"
down_revision = "20261017_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.alter_column(
            "verified_by",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.alter_column(
            "verified_by",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=True,
        )
