import runpy
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.models import Base, TransferJob


def test_migration_chain_matches_current_schema_and_downgrades(tmp_path):
    migration_dir = Path(__file__).parents[1] / "migrations/versions"
    migrations = [
        runpy.run_path(str(path))
        for path in sorted(migration_dir.glob("*.py"))
        if path.name != "__init__.py"
    ]
    scheduling_index = next(
        index
        for index, migration in enumerate(migrations)
        if migration["revision"] == "20261013_0008"
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        with Operations.context(context):
            for migration in migrations:
                migration["upgrade"]()

        manifest_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("transfer_manifest_items")
        }
        assert (
            manifest_columns["verified_by"]["type"].length >= len("SHA256_RECONCILED")
        )

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
            for migration in reversed(migrations[scheduling_index:]):
                migration["downgrade"]()
        assert connection.scalar(
            select(TransferJob.status).where(TransferJob.id == scheduled_transfer_id)
        ) == "QUEUED"

        with Operations.context(MigrationContext.configure(connection)):
            for migration in reversed(migrations[:scheduling_index]):
                migration["downgrade"]()

        assert inspect(connection).get_table_names() == []

    engine.dispose()


def test_omission_migration_preserves_legacy_skipped_manifest_rows(tmp_path):
    migration_dir = Path(__file__).parents[1] / "migrations/versions"
    migrations = [
        runpy.run_path(str(path))
        for path in sorted(migration_dir.glob("*.py"))
        if path.name != "__init__.py"
    ]
    migration_index = next(
        index for index, item in enumerate(migrations) if item["revision"] == "20261018_0015"
    )
    migration = migrations[migration_index]
    previous = migrations[:migration_index]
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-omission.sqlite'}")
    transfer_id = "18a72d94-b7b1-4ecb-b77a-33cd7891e525"
    item_id = "7bf9ff3e-9e5b-4c73-b2f6-4a58bc2f64f7"

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for upgrade in previous:
                upgrade["upgrade"]()
        connection.execute(
            text(
                "INSERT INTO transfer_manifest_items "
                "(id, transfer_job_id, source_path, destination_path, source_path_digest, "
                "is_dir, size_bytes, status, copy_started, destination_existed_before, "
                "scan_complete) VALUES (:id, :transfer_id, '/legacy.rar', '/archive/legacy.rar', "
                ":digest, 0, 77, 'SKIPPED', 0, 0, 1)"
            ),
            {"id": item_id, "transfer_id": transfer_id, "digest": "a" * 64},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()
        row = connection.execute(
            text(
                "SELECT status, size_bytes, copy_started, omission_kind, omitted_at, omitted_by "
                "FROM transfer_manifest_items WHERE id = :id"
            ),
            {"id": item_id},
        ).one()

    assert tuple(row) == ("SKIPPED", 77, 0, None, None, None)
    engine.dispose()


def test_omission_migration_downgrade_refuses_to_erase_explicit_omissions(tmp_path):
    migration_dir = Path(__file__).parents[1] / "migrations/versions"
    migrations = [
        runpy.run_path(str(path))
        for path in sorted(migration_dir.glob("*.py"))
        if path.name != "__init__.py"
    ]
    migration_index = next(
        index for index, item in enumerate(migrations) if item["revision"] == "20261018_0015"
    )
    migration = migrations[migration_index]
    previous = migrations[:migration_index]
    engine = create_engine(f"sqlite:///{tmp_path / 'explicit-omission-downgrade.sqlite'}")
    transfer_id = "18a72d94-b7b1-4ecb-b77a-33cd7891e525"
    item_id = "7bf9ff3e-9e5b-4c73-b2f6-4a58bc2f64f7"

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for upgrade in previous:
                upgrade["upgrade"]()
            migration["upgrade"]()
        connection.execute(
            text(
                "INSERT INTO transfer_manifest_items "
                "(id, transfer_job_id, source_path, destination_path, source_path_digest, "
                "is_dir, size_bytes, status, copy_started, destination_existed_before, "
                "scan_complete, omission_kind, omitted_at) VALUES (:id, :transfer_id, "
                "'/restricted.rar', '/archive/restricted.rar', :digest, 0, 77, 'SKIPPED', "
                "0, 0, 1, 'USER_ACCEPTED_SOURCE_FAILURE', '2026-10-18T12:00:00Z')"
            ),
            {"id": item_id, "transfer_id": transfer_id, "digest": "b" * 64},
        )
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="explicit transfer omissions"):
                migration["downgrade"]()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("transfer_manifest_items")
        }
        row = connection.execute(
            text(
                "SELECT status, omission_kind FROM transfer_manifest_items WHERE id = :id"
            ),
            {"id": item_id},
        ).one()

    assert "omission_kind" in columns
    assert tuple(row) == ("SKIPPED", "USER_ACCEPTED_SOURCE_FAILURE")
    engine.dispose()


def test_pause_migration_downgrade_refuses_to_remove_live_pause_states(tmp_path):
    migration_dir = Path(__file__).parents[1] / "migrations/versions"
    migrations = [
        runpy.run_path(str(path))
        for path in sorted(migration_dir.glob("*.py"))
        if path.name != "__init__.py"
    ]
    migration = next(item for item in migrations if item["revision"] == "20261018_0017")
    engine = create_engine(f"sqlite:///{tmp_path / 'paused-transfer-downgrade.sqlite'}")
    transfer_id = uuid.UUID("b3cbda67-13a5-493e-88c9-bff17e0566bc")

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for upgrade in migrations:
                upgrade["upgrade"]()
        connection.execute(
            TransferJob.__table__.insert().values(
                id=transfer_id,
                public_id="TRF-2026-000100",
                requested_by=uuid.UUID("e267fe79-086e-4519-a749-5be01e034efe"),
                source_connection_id=uuid.UUID("f0596305-8513-4174-918f-76cda3dc5869"),
                destination_connection_id=uuid.UUID("368b08ea-00a3-443e-8d64-99221d2eab96"),
                source_paths=["/source"],
                destination_path="/destination",
                status="PAUSED",
                pause_requested_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
                paused_at=datetime(2026, 10, 18, 12, 1, tzinfo=UTC),
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="PAUSING or PAUSED"):
                migration["downgrade"]()
        columns = {column["name"] for column in inspect(connection).get_columns("transfer_jobs")}
        status = connection.scalar(
            select(TransferJob.status).where(TransferJob.id == transfer_id)
        )

    assert {"pause_requested_at", "paused_at", "state_version"}.issubset(columns)
    assert status == "PAUSED"
    engine.dispose()
