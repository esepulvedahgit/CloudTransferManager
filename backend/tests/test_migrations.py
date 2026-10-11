import runpy
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models import Base


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

        with Operations.context(MigrationContext.configure(connection)):
            for migration in reversed(migrations):
                migration["downgrade"]()

        assert inspect(connection).get_table_names() == []

    engine.dispose()
