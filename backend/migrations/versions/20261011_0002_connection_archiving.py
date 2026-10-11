"""Add logical archiving to storage connections.

Revision ID: 20261011_0002
Revises: 20261010_0001
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0002"
down_revision = "20261010_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storage_connections",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storage_connections", "deleted_at")
