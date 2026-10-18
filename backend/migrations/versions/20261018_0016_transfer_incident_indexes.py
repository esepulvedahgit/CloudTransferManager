"""Index recent failed files and explicit omissions for the attention panel.

Revision ID: 20261018_0016
Revises: 20261018_0015
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0016"
down_revision = "20261018_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_transfer_incident_failed",
        "transfer_manifest_items",
        ["updated_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'FAILED' AND is_dir = false"),
        sqlite_where=sa.text("status = 'FAILED' AND is_dir = 0"),
    )
    op.create_index(
        "ix_transfer_incident_omitted",
        "transfer_manifest_items",
        ["omitted_at", "id"],
        unique=False,
        postgresql_where=sa.text("omission_kind IS NOT NULL AND is_dir = false"),
        sqlite_where=sa.text("omission_kind IS NOT NULL AND is_dir = 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_transfer_incident_omitted", table_name="transfer_manifest_items")
    op.drop_index("ix_transfer_incident_failed", table_name="transfer_manifest_items")
