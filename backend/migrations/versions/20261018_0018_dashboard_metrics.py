"""Store transfer copy dates and measured execution spans.

Revision ID: 20261018_0018
Revises: 20261018_0017
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0018"
down_revision = "20261018_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transfer_manifest_items",
        sa.Column("copied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "transfer_metric_spans",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("transfer_job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("manifest_item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("dispatch_token", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("span_type", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=True),
        sa.Column("connection_role", sa.String(length=16), nullable=True),
        sa.Column("wait_reason", sa.String(length=64), nullable=True),
        sa.Column("connection_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("bytes_count", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("span_type IN ('STATE', 'STAGE')", name="ck_transfer_metric_span_type"),
        sa.CheckConstraint(
            "outcome IN ('OPEN', 'SUCCESS', 'FAILED', 'INTERRUPTED')",
            name="ck_transfer_metric_outcome",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_transfer_metric_duration"
        ),
        sa.CheckConstraint(
            "bytes_count IS NULL OR bytes_count >= 0", name="ck_transfer_metric_bytes"
        ),
        sa.ForeignKeyConstraint(["transfer_job_id"], ["transfer_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manifest_item_id"], ["transfer_manifest_items.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["storage_connections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transfer_metric_time",
        "transfer_metric_spans",
        ["span_type", "code", "started_at"],
    )
    op.create_index(
        "ix_transfer_metric_transfer",
        "transfer_metric_spans",
        ["transfer_job_id", "started_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    has_spans = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM transfer_metric_spans)")
    ).scalar()
    has_copy_dates = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM transfer_manifest_items WHERE copied_at IS NOT NULL)")
    ).scalar()
    if has_spans or has_copy_dates:
        raise RuntimeError(
            "Dashboard metrics contain durable measurements; archive them before downgrading."
        )
    op.drop_index("ix_transfer_metric_transfer", table_name="transfer_metric_spans")
    op.drop_index("ix_transfer_metric_time", table_name="transfer_metric_spans")
    op.drop_table("transfer_metric_spans")
    op.drop_column("transfer_manifest_items", "copied_at")
