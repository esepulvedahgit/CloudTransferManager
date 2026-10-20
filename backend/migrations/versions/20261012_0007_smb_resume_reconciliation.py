"""Track ambiguous SMB uploads for safe resume reconciliation.

Revision ID: 20261012_0007
Revises: 20261012_0006
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0007"
down_revision = "20261012_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.add_column(
            sa.Column("copy_started", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "destination_existed_before",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.drop_column("destination_existed_before")
        batch_op.drop_column("copy_started")
