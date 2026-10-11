"""Add Seafile and per-connection adapter selection.

Revision ID: 20261011_0003
Revises: 20261011_0002
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0003"
down_revision = "20261011_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE')",
        )
        batch_op.add_column(
            sa.Column("adapter_type", sa.String(length=32), server_default="RCLONE", nullable=False)
        )
        batch_op.create_check_constraint(
            "ck_storage_adapter_type", "adapter_type IN ('RCLONE', 'SEAFILE_API')"
        )


def downgrade() -> None:
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_adapter_type", type_="check")
        batch_op.drop_column("adapter_type")
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE')",
        )
