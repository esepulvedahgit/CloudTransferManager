import runpy
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from app.models import Base, TransferJob


def test_migration_chain_matches_current_schema_and_downgrades(tmp_path):
    migration_dir = Path(__file__).parents[1] / "migrations/versions"
    migrations = [
        runpy.run_path(str(path))
        for path in sorted(migration_dir.glob("*.py"))
        if path.name != "__init__.py"
    ]
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        with Operations.context(context):
            for migration in migrations:
                migration["upgrade"]()

        differences = compare_metadata(
            MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata
        )
        assert differences == []
        assert "audit_events" in inspect(connection).get_table_names()

        scheduled_transfer_id = uuid.UUID("2c0d4aef-4cab-4d27-b708-6bb2dc080a58")
        connection.execute(
            TransferJob.__table__.insert().values(
                id=scheduled_transfer_id,
                public_id="TRF-2026-000099",
                requested_by=uuid.UUID("e267fe79-086e-4519-a749-5be01e034efe"),
                source_connection_id=uuid.UUID("f0596305-8513-4174-918f-76cda3dc5869"),
                destination_connection_id=uuid.UUID("368b08ea-00a3-443e-8d64-99221d2eab96"),
                source_paths=["/source"],
                destination_path="/destination",
                status="SCHEDULED",
                scheduled_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migrations[-1]["downgrade"]()
            migrations[-2]["downgrade"]()
        assert connection.scalar(
            select(TransferJob.status).where(TransferJob.id == scheduled_transfer_id)
        ) == "QUEUED"

        with Operations.context(MigrationContext.configure(connection)):
            for migration in reversed(migrations[:-2]):
                migration["downgrade"]()

        assert inspect(connection).get_table_names() == []

    engine.dispose()
