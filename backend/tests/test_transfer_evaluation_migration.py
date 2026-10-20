import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    create_engine,
    inspect,
)
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
from sqlalchemy.types import BigInteger, Text, Uuid

from app.settings import get_settings


def _required_values(table, overrides):
    values = dict(overrides)
    for column in table.columns:
        if column.name in values or column.nullable or column.server_default is not None:
            continue
        column_type = column.type
        if isinstance(column_type, Uuid):
            values[column.name] = uuid.uuid4()
        elif isinstance(column_type, Boolean):
            values[column.name] = False
        elif isinstance(column_type, (Integer, BigInteger, Numeric)):
            values[column.name] = 0
        elif isinstance(column_type, (JSON, SQLiteJSON)):
            values[column.name] = [] if "path" in column.name else {}
        elif isinstance(column_type, DateTime):
            values[column.name] = datetime.now(UTC)
        elif isinstance(column_type, LargeBinary):
            values[column.name] = b"test"
        elif isinstance(column_type, (String, Text)):
            values[column.name] = "test"
        else:
            raise AssertionError(f"No test value for required column {column.name}")
    return {
        name: value.hex if isinstance(value, uuid.UUID) else value
        for name, value in values.items()
    }


def test_migration_backfills_only_unclaimed_scheduled_transfers(tmp_path, monkeypatch):
    database_path = tmp_path / "evaluation-migration.sqlite"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "backend" / "migrations"))
    config.set_main_option("path_separator", "os")

    try:
        command.upgrade(config, "20261016_0012")
        engine = create_engine(f"sqlite:///{database_path}")
        metadata = MetaData()
        metadata.reflect(engine)
        user_id, source_id, destination_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        eligible_scheduled, eligible_queued, claimed, terminal = (uuid.uuid4() for _ in range(4))
        scheduled_at = datetime.now(UTC) + timedelta(hours=1)
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["users"].insert().values(
                    **_required_values(
                        metadata.tables["users"],
                        {
                            "id": user_id,
                            "username": "migration-owner",
                            "email": "migration@example.test",
                            "password_hash": "hash",
                            "role": "ADMIN",
                            "is_active": True,
                            "must_change_password": False,
                            "failed_login_attempts": 0,
                        },
                    )
                )
            )
            for connection_id, name in ((source_id, "Source"), (destination_id, "Destination")):
                connection.execute(
                    metadata.tables["storage_connections"].insert().values(
                        **_required_values(
                            metadata.tables["storage_connections"],
                            {
                                "id": connection_id,
                                "name": name,
                                "provider_type": "MOCK",
                                "status": "ACTIVE",
                                "encrypted_credentials": b"",
                                "credential_version": 1,
                                "is_global": True,
                                "created_by": user_id,
                            },
                        )
                    )
                )
            transfers = metadata.tables["transfer_jobs"]
            transfer_cases = [
                (eligible_scheduled, "SCHEDULED", None, None),
                (eligible_queued, "QUEUED", None, None),
                (claimed, "QUEUED", uuid.uuid4(), None),
                (terminal, "FAILED", None, None),
            ]
            for index, (transfer_id, status, dispatch_token, execution_started_at) in enumerate(
                transfer_cases
            ):
                connection.execute(
                    transfers.insert().values(
                        **_required_values(
                            transfers,
                            {
                                "id": transfer_id,
                                "public_id": f"TRF-2026-{index + 1:06d}",
                                "requested_by": user_id,
                                "source_connection_id": source_id,
                                "destination_connection_id": destination_id,
                                "source_paths": ["/folder"],
                                "destination_path": "/archive",
                                "operation": "COPY",
                                "conflict_policy": "KEEP_BOTH",
                                "send_email_notification": False,
                                "status": status,
                                "scheduled_at": scheduled_at,
                                "dispatch_token": dispatch_token,
                                "execution_started_at": execution_started_at,
                                "source_credential_version": 1,
                                "destination_credential_version": 1,
                            },
                        )
                    )
                )
        engine.dispose()

        command.upgrade(config, "head")

        verification_engine = create_engine(f"sqlite:///{database_path}")
        with verification_engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT transfer_job_id, status FROM transfer_evaluations"
            ).all()
        command.downgrade(config, "20261016_0012")
        with verification_engine.connect() as connection:
            evaluation_table_exists = inspect(connection).has_table("transfer_evaluations")
        verification_engine.dispose()
    finally:
        get_settings.cache_clear()

    assert {uuid.UUID(row[0]) for row in rows} == {eligible_scheduled, eligible_queued}
    assert all(row[1] == "PENDING" for row in rows)
    assert not evaluation_table_exists
