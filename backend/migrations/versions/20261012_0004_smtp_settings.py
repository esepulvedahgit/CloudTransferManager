"""Add global SMTP settings storage.

Revision ID: 20261012_0004
Revises: 20261011_0003
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "20261012_0004"
down_revision = "20261011_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "smtp_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), server_default="global", nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("use_tls", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("from_address", sa.String(length=255), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("scope = 'global'", name="ck_smtp_settings_global_scope"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("smtp_settings")
