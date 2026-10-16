"""Persist stable source identity and universal destination assignments.

Revision ID: 20261016_0012
Revises: 20261014_0011
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261016_0012"
down_revision = "20261014_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_item_ids",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_parent_ids",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )

    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.add_column(sa.Column("source_object_id", sa.String(length=512), nullable=True))
        batch_op.add_column(
            sa.Column("requested_destination_path", sa.String(length=2048), nullable=True)
        )
        batch_op.add_column(sa.Column("rename_reason", sa.String(length=32), nullable=True))

    op.execute(
        "UPDATE transfer_manifest_items "
        "SET requested_destination_path = destination_path "
        "WHERE requested_destination_path IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.drop_column("rename_reason")
        batch_op.drop_column("requested_destination_path")
        batch_op.drop_column("source_object_id")
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_column("source_parent_ids")
        batch_op.drop_column("source_item_ids")
