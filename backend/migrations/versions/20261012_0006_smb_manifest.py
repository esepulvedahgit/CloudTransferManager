"""Persist SMB transfer inventory and connection credential snapshots.

Revision ID: 20261012_0006
Revises: 20261012_0005
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0006"
down_revision = "20261012_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("source_credential_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column(
                "destination_credential_version", sa.Integer(), nullable=False, server_default="1"
            )
        )

    op.create_table(
        "transfer_manifest_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_job_id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.String(length=2048), nullable=False),
        sa.Column("destination_path", sa.String(length=2048), nullable=False),
        sa.Column("source_path_digest", sa.String(length=64), nullable=False),
        sa.Column("is_dir", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_complete", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("verified_by", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('DIRECTORY', 'PENDING', 'COPIED', 'SKIPPED', 'FAILED')",
            name="ck_transfer_manifest_status",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_job_id"], ["transfer_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transfer_job_id", "source_path_digest", name="uq_transfer_manifest_source"
        ),
    )
    op.create_index(
        "ix_transfer_manifest_scan",
        "transfer_manifest_items",
        ["transfer_job_id", "is_dir", "scan_complete"],
    )
    op.create_index(
        "ix_transfer_manifest_status",
        "transfer_manifest_items",
        ["transfer_job_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transfer_manifest_status", table_name="transfer_manifest_items")
    op.drop_index("ix_transfer_manifest_scan", table_name="transfer_manifest_items")
    op.drop_table("transfer_manifest_items")
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_column("destination_credential_version")
        batch_op.drop_column("source_credential_version")
