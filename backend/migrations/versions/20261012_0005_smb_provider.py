"""Allow SMB private storage connections.

Revision ID: 20261012_0005
Revises: 20261012_0004
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0005"
down_revision = "20261012_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB')",
        )


def downgrade() -> None:
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE')",
        )
