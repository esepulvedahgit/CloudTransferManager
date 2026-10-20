"""Allow Nextcloud storage connections.

Revision ID: 20261013_0009
Revises: 20261013_0008
Create Date: 2026-10-13
"""

from alembic import op
from sqlalchemy import text

revision = "20261013_0009"
down_revision = "20261013_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB', 'NEXTCLOUD')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    has_nextcloud = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM storage_connections WHERE provider_type = 'NEXTCLOUD')")
    ).scalar_one()
    if has_nextcloud:
        raise RuntimeError("Cannot downgrade while Nextcloud connections exist")
    with op.batch_alter_table("storage_connections") as batch_op:
        batch_op.drop_constraint("ck_storage_provider_type", type_="check")
        batch_op.create_check_constraint(
            "ck_storage_provider_type",
            "provider_type IN ('MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB')",
        )
