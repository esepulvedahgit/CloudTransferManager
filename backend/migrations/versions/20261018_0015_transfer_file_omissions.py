"""Persist explicit transfer file omissions and retry evidence.

Revision ID: 20261018_0015
Revises: 20261017_0014
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0015"
down_revision = "20261017_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_file_error_policy",
                sa.String(length=32),
                nullable=False,
                server_default="STOP",
            )
        )
        batch_op.add_column(
            sa.Column("accepted_parent_omissions_attempt", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_transfer_source_file_error_policy",
            "source_file_error_policy IN ('STOP', 'SKIP_CONFIRMED_RESTRICTED')",
        )
        batch_op.create_check_constraint(
            "ck_transfer_accepted_parent_omissions_attempt",
            "accepted_parent_omissions_attempt IS NULL OR "
            "accepted_parent_omissions_attempt >= 1",
        )

    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.add_column(sa.Column("omission_kind", sa.String(length=40), nullable=True))
        batch_op.add_column(
            sa.Column("omitted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("omitted_by", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("last_failure_stage", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_failure_attempt", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_transfer_manifest_items_omitted_by_users",
            "users",
            ["omitted_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_transfer_manifest_omission_kind",
            "omission_kind IS NULL OR omission_kind IN "
            "('USER_ACCEPTED_SOURCE_FAILURE', 'MEGA_FILE_RESTRICTED')",
        )
        batch_op.create_check_constraint(
            "ck_transfer_manifest_explicit_omission",
            "omission_kind IS NULL OR (is_dir = false AND status = 'SKIPPED' "
            "AND omitted_at IS NOT NULL AND copy_started = false)",
        )
        batch_op.create_check_constraint(
            "ck_transfer_manifest_omission_metadata",
            "omission_kind IS NOT NULL OR (omitted_at IS NULL AND omitted_by IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_transfer_manifest_last_failure_attempt",
            "last_failure_attempt IS NULL OR last_failure_attempt >= 1",
        )


def downgrade() -> None:
    explicit_omissions = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM transfer_manifest_items "
            "WHERE omission_kind IS NOT NULL"
        )
    ).scalar_one()
    if explicit_omissions:
        raise RuntimeError(
            "Cannot downgrade while explicit transfer omissions exist; "
            "the older executor would retry those files."
        )

    with op.batch_alter_table("transfer_manifest_items") as batch_op:
        batch_op.drop_constraint("ck_transfer_manifest_last_failure_attempt", type_="check")
        batch_op.drop_constraint("ck_transfer_manifest_omission_metadata", type_="check")
        batch_op.drop_constraint("ck_transfer_manifest_explicit_omission", type_="check")
        batch_op.drop_constraint("ck_transfer_manifest_omission_kind", type_="check")
        batch_op.drop_constraint("fk_transfer_manifest_items_omitted_by_users", type_="foreignkey")
        batch_op.drop_column("last_failure_attempt")
        batch_op.drop_column("last_failure_stage")
        batch_op.drop_column("omitted_by")
        batch_op.drop_column("omitted_at")
        batch_op.drop_column("omission_kind")

    with op.batch_alter_table("transfer_jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_transfer_accepted_parent_omissions_attempt", type_="check"
        )
        batch_op.drop_constraint("ck_transfer_source_file_error_policy", type_="check")
        batch_op.drop_column("accepted_parent_omissions_attempt")
        batch_op.drop_column("source_file_error_policy")
