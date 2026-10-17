"""Persist early evaluation state for scheduled transfers.

Revision ID: 20261017_0013
Revises: 20261016_0012
Create Date: 2026-10-17
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "20261017_0013"
down_revision = "20261016_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transfer_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("files_found", sa.BigInteger(), nullable=True),
        sa.Column("count_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("max_files", sa.BigInteger(), nullable=True),
        sa.Column("source_credential_version", sa.Integer(), nullable=True),
        sa.Column("destination_credential_version", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_message", sa.String(length=512), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'EVALUATING', 'APPROVED', 'BLOCKED', 'ERROR', 'WAITING_PARENT')",
            name="ck_transfer_evaluation_status",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_transfer_evaluation_generation"),
        sa.CheckConstraint("sequence >= 0", name="ck_transfer_evaluation_sequence"),
        sa.CheckConstraint(
            "files_found IS NULL OR files_found >= 0", name="ck_evaluation_files_found"
        ),
        sa.CheckConstraint("max_files IS NULL OR max_files >= 1", name="ck_evaluation_max_files"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_evaluation_attempt_count"),
        sa.ForeignKeyConstraint(
            ["transfer_job_id"], ["transfer_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transfer_job_id", name="uq_transfer_evaluation_transfer"),
    )
    op.create_index(
        "ix_transfer_evaluation_dispatch",
        "transfer_evaluations",
        ["status", "retry_at", "updated_at"],
    )

    connection = op.get_bind()
    evaluation_table = sa.table(
        "transfer_evaluations",
        sa.column("id", sa.Uuid()),
        sa.column("transfer_job_id", sa.Uuid()),
        sa.column("status", sa.String(length=24)),
        sa.column("generation", sa.Integer()),
        sa.column("sequence", sa.Integer()),
        sa.column("count_complete", sa.Boolean()),
        sa.column("source_credential_version", sa.Integer()),
        sa.column("destination_credential_version", sa.Integer()),
        sa.column("attempt_count", sa.Integer()),
    )
    pending_transfers = connection.execute(
        sa.text(
            "SELECT id, source_credential_version, destination_credential_version "
            "FROM transfer_jobs "
            "WHERE scheduled_at IS NOT NULL AND status IN ('SCHEDULED', 'QUEUED') "
            "AND dispatch_token IS NULL AND execution_started_at IS NULL"
        )
    ).mappings()
    for transfer in pending_transfers:
        connection.execute(
            evaluation_table.insert().values(
                id=uuid.uuid4(),
                transfer_job_id=uuid.UUID(str(transfer["id"])),
                status="PENDING",
                generation=1,
                sequence=0,
                count_complete=False,
                source_credential_version=transfer["source_credential_version"],
                destination_credential_version=transfer["destination_credential_version"],
                attempt_count=0,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_transfer_evaluation_dispatch", table_name="transfer_evaluations")
    op.drop_table("transfer_evaluations")
