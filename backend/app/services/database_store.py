import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    AuditEvent,
    Base,
    SMTPSettings,
    StorageConnection,
    TransferCounter,
    TransferEvent,
    TransferJob,
    TransferManifestItem,
    User,
    UserConnectionGrant,
)
from app.security.sanitize import sanitize_data
from app.services.memory_store import (
    ACTIVE_TRANSFER_STATUSES,
    ConnectionInUseError,
    ConnectionRecord,
    ConnectionUnavailableError,
    SMTPSettingsRecord,
    TransferManifestItemRecord,
    TransferRecord,
    UserAccount,
)
from app.settings import Settings, get_settings


class DatabaseStore:
    """PostgreSQL-backed implementation of the application repository."""

    def __init__(self, settings: Settings | None = None) -> None:
        config = settings or get_settings()
        self.engine = create_async_engine(config.resolved_database_url(), pool_pre_ping=True)
        self.sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_schema(self) -> None:
        """Create schema for isolated tests; deployments use Alembic migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def acheck(self) -> bool:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    @asynccontextmanager
    async def transfer_execution_lock(self, transfer_id: uuid.UUID) -> AsyncIterator[bool]:
        """Hold a PostgreSQL session lock for the full lifetime of one worker task."""
        async with self.engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                yield True
                return

            lock_key = int.from_bytes(
                hashlib.sha256(transfer_id.bytes).digest()[:8], "big", signed=True
            )
            lock_parameters = {"lock_key": lock_key}
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"), lock_parameters
                )
            )
            await connection.commit()
            if not acquired:
                yield False
                return

            try:
                yield True
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), lock_parameters
                )
                await connection.commit()

    @staticmethod
    def _smtp_settings(row: SMTPSettings) -> SMTPSettingsRecord:
        return SMTPSettingsRecord(
            id=row.id,
            host=row.host,
            port=row.port,
            use_tls=row.use_tls,
            from_address=row.from_address,
            encrypted_credentials=bytes(row.encrypted_credentials),
        )

    async def aget_smtp_settings(self) -> SMTPSettingsRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(select(SMTPSettings).where(SMTPSettings.scope == "global"))
            return self._smtp_settings(row) if row else None

    async def acreate_smtp_settings_if_absent(
        self, settings: SMTPSettingsRecord
    ) -> SMTPSettingsRecord:
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(SMTPSettings).values(
                id=settings.id,
                scope="global",
                host=settings.host,
                port=settings.port,
                use_tls=settings.use_tls,
                from_address=settings.from_address,
                encrypted_credentials=settings.encrypted_credentials,
            )
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=[SMTPSettings.scope])
            )
            row = await session.scalar(select(SMTPSettings).where(SMTPSettings.scope == "global"))
            if row is None:
                raise RuntimeError("SMTP settings reservation failed")
            await session.commit()
            return self._smtp_settings(row)

    async def asave_smtp_settings(self, settings: SMTPSettingsRecord) -> SMTPSettingsRecord:
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(SMTPSettings).values(
                id=settings.id,
                scope="global",
                host=settings.host,
                port=settings.port,
                use_tls=settings.use_tls,
                from_address=settings.from_address,
                encrypted_credentials=settings.encrypted_credentials,
            )
            upsert = statement.on_conflict_do_update(
                index_elements=[SMTPSettings.scope],
                set_={
                    "host": settings.host,
                    "port": settings.port,
                    "use_tls": settings.use_tls,
                    "from_address": settings.from_address,
                    "encrypted_credentials": settings.encrypted_credentials,
                },
            ).returning(SMTPSettings)
            row = (await session.scalars(upsert)).one()
            await session.commit()
            return self._smtp_settings(row)

    @staticmethod
    def _user(row: User) -> UserAccount:
        return UserAccount(
            id=row.id,
            username=row.username,
            email=row.email,
            password_hash=row.password_hash,
            role=row.role,
            is_active=row.is_active,
            must_change_password=row.must_change_password,
            failed_login_attempts=row.failed_login_attempts,
            locked_until=row.locked_until,
            last_login_at=row.last_login_at,
        )

    @staticmethod
    def _connection(row: StorageConnection) -> ConnectionRecord:
        return ConnectionRecord(
            id=row.id,
            name=row.name,
            provider_type=row.provider_type,
            adapter_type=row.adapter_type,
            created_by=row.created_by,
            encrypted_credentials=bytes(row.encrypted_credentials),
            status=row.status,
            credential_version=row.credential_version,
            last_tested_at=row.last_tested_at,
            last_test_status=row.last_test_status,
            is_global=row.is_global,
            deleted_at=row.deleted_at,
        )

    @staticmethod
    def _transfer(row: TransferJob) -> TransferRecord:
        return TransferRecord(
            id=row.id,
            public_id=row.public_id,
            requested_by=row.requested_by,
            source_connection_id=row.source_connection_id,
            destination_connection_id=row.destination_connection_id,
            source_credential_version=row.source_credential_version,
            destination_credential_version=row.destination_credential_version,
            source_paths=list(row.source_paths),
            destination_path=row.destination_path,
            conflict_policy=row.conflict_policy,
            send_email_notification=row.send_email_notification,
            status=row.status,
            progress_percent=float(row.progress_percent or 0),
            files_total=row.files_total or 0,
            files_completed=row.files_completed,
            bytes_total=row.bytes_total or 0,
            bytes_transferred=row.bytes_transferred,
            current_item=row.current_item,
            average_speed_bps=row.average_speed_bps,
            current_speed_bps=row.current_speed_bps,
            estimated_remaining_seconds=row.estimated_remaining_seconds,
            attempt_count=row.attempt_count,
            error_count=row.error_count,
            warning_count=row.warning_count,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _manifest_item(row: TransferManifestItem) -> TransferManifestItemRecord:
        return TransferManifestItemRecord(
            id=row.id,
            transfer_id=row.transfer_job_id,
            source_path=row.source_path,
            destination_path=row.destination_path,
            is_dir=row.is_dir,
            size_bytes=row.size_bytes,
            modified_at=row.modified_at,
            scan_complete=row.scan_complete,
            status=row.status,
            copy_started=row.copy_started,
            destination_existed_before=row.destination_existed_before,
            error_code=row.error_code,
            verified_by=row.verified_by,
        )

    async def acreate_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        async with self.sessions() as session:
            row = User(
                username=username,
                email=email,
                password_hash=password_hash,
                role=role,
                is_active=is_active,
                must_change_password=must_change_password,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._user(row)

    async def aget_user(self, user_id: uuid.UUID) -> UserAccount | None:
        async with self.sessions() as session:
            row = await session.get(User, user_id)
            return self._user(row) if row else None

    async def alist_users(self) -> list[UserAccount]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(User).order_by(User.username))).all()
            return [self._user(row) for row in rows]

    async def acount_active_admins(self) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.is_active.is_(True), User.role == "ADMIN")
                )
                or 0
            )

    async def afind_user(self, username_or_email: str) -> UserAccount | None:
        normalized = username_or_email.casefold()
        async with self.sessions() as session:
            row = await session.scalar(
                select(User).where(
                    or_(
                        func.lower(User.username) == normalized,
                        func.lower(User.email) == normalized,
                    )
                )
            )
            return self._user(row) if row else None

    async def asave_user(self, user: UserAccount) -> None:
        async with self.sessions() as session:
            row = await session.get(User, user.id)
            if row is None:
                return
            row.failed_login_attempts = user.failed_login_attempts
            row.locked_until = user.locked_until
            row.last_login_at = user.last_login_at
            row.is_active = user.is_active
            row.must_change_password = user.must_change_password
            row.password_hash = user.password_hash
            row.role = user.role
            row.username = user.username
            row.email = user.email
            await session.commit()

    async def acreate_connection(
        self,
        name: str,
        provider_type: str,
        created_by: uuid.UUID,
        connection_id: uuid.UUID,
        encrypted_credentials: bytes,
        is_global: bool = True,
        adapter_type: str = "RCLONE",
    ) -> ConnectionRecord:
        async with self.sessions() as session:
            row = StorageConnection(
                id=connection_id,
                name=name,
                provider_type=provider_type,
                adapter_type=adapter_type,
                created_by=created_by,
                encrypted_credentials=encrypted_credentials,
                is_global=is_global,
                status="ACTIVE",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._connection(row)

    async def aget_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(StorageConnection).where(
                    StorageConnection.id == connection_id,
                    StorageConnection.deleted_at.is_(None),
                )
            )
            return self._connection(row) if row else None

    async def aarchive_connection(
        self,
        connection_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectionRecord | None:
        async with self.sessions() as session:
            async with session.begin():
                connection = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update()
                )
                if connection is None or connection.deleted_at is not None:
                    return None
                active_transfer = await session.scalar(
                    select(TransferJob.id)
                    .where(
                        TransferJob.status.in_(ACTIVE_TRANSFER_STATUSES),
                        or_(
                            TransferJob.source_connection_id == connection_id,
                            TransferJob.destination_connection_id == connection_id,
                        ),
                    )
                    .limit(1)
                )
                if active_transfer is not None:
                    raise ConnectionInUseError("Connection has an active transfer")
                connection.deleted_at = datetime.now(UTC)
                connection.encrypted_credentials = b""
                await session.execute(
                    delete(UserConnectionGrant).where(
                        UserConnectionGrant.connection_id == connection_id
                    )
                )
                session.add(
                    AuditEvent(
                        actor_user_id=actor_user_id,
                        action="CONNECTION_ARCHIVED",
                        resource_type="connection",
                        resource_id=str(connection.id),
                        result="SUCCESS",
                        event_metadata=sanitize_data(
                            {
                                "provider_type": connection.provider_type,
                                "is_global": connection.is_global,
                            }
                        ),
                        ip_address=ip_address,
                        user_agent=user_agent[:255] if user_agent else None,
                    )
                )
            return self._connection(connection)

    async def alist_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        async with self.sessions() as session:
            statement = (
                select(StorageConnection)
                .where(StorageConnection.deleted_at.is_(None))
                .order_by(StorageConnection.name)
            )
            if user_id is not None:
                granted = select(UserConnectionGrant.connection_id).where(
                    UserConnectionGrant.user_id == user_id
                )
                statement = statement.where(
                    or_(StorageConnection.is_global.is_(True), StorageConnection.id.in_(granted))
                )
            rows = (await session.scalars(statement)).all()
            return [self._connection(row) for row in rows]

    async def ahas_connection_access(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        async with self.sessions() as session:
            connection = await session.get(StorageConnection, connection_id)
            if connection is None or connection.deleted_at is not None:
                return False
            if connection.is_global:
                return True
            return await session.get(UserConnectionGrant, (user_id, connection_id)) is not None

    async def agrant_connection(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, granted_by: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                connection = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update()
                )
                if connection is None or connection.deleted_at is not None:
                    raise ConnectionUnavailableError("not_found")
                existing = await session.get(UserConnectionGrant, (user_id, connection_id))
                if existing is None:
                    session.add(
                        UserConnectionGrant(
                            user_id=user_id, connection_id=connection_id, granted_by=granted_by
                        )
                    )

    async def arevoke_connection(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            grant = await session.get(UserConnectionGrant, (user_id, connection_id))
            if grant is not None:
                await session.delete(grant)
                await session.commit()

    async def asave_connection(self, connection: ConnectionRecord) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection.id)
                    .with_for_update()
                )
                if row is None or row.deleted_at is not None:
                    raise ConnectionUnavailableError("not_found")
                row.name = connection.name
                row.adapter_type = connection.adapter_type
                row.status = connection.status
                row.last_tested_at = connection.last_tested_at
                row.last_test_status = connection.last_test_status
                row.is_global = connection.is_global
                if bytes(row.encrypted_credentials) != connection.encrypted_credentials:
                    row.credential_version += 1
                row.encrypted_credentials = connection.encrypted_credentials
                connection.credential_version = row.credential_version

    async def _next_public_id(self, session: AsyncSession) -> str:
        year = datetime.now(UTC).year
        dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
        statement: Any
        if dialect == "postgresql":
            statement = pg_insert(TransferCounter).values(year=year, last_value=1)
        else:
            statement = sqlite_insert(TransferCounter).values(year=year, last_value=1)
        statement = statement.on_conflict_do_update(
            index_elements=[TransferCounter.year],
            set_={"last_value": TransferCounter.last_value + 1},
        ).returning(TransferCounter.last_value)
        number = await session.scalar(statement)
        return f"TRF-{year}-{int(number or 1):06d}"

    async def acreate_transfer(
        self,
        requested_by: uuid.UUID,
        source_connection_id: uuid.UUID,
        destination_connection_id: uuid.UUID,
        source_paths: list[str],
        destination_path: str,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
        send_email_notification: bool = False,
    ) -> TransferRecord:
        async with self.sessions() as session:
            connection_ids = sorted({source_connection_id, destination_connection_id})
            async with session.begin():
                connections = (
                    await session.scalars(
                        select(StorageConnection)
                        .where(StorageConnection.id.in_(connection_ids))
                        .order_by(StorageConnection.id)
                        .with_for_update()
                    )
                ).all()
                by_id = {connection.id: connection for connection in connections}
                for connection_id in connection_ids:
                    connection = by_id.get(connection_id)
                    if connection is None or connection.deleted_at is not None:
                        raise ConnectionUnavailableError("not_found")
                    if connection.status != "ACTIVE":
                        raise ConnectionUnavailableError("inactive")
                row = TransferJob(
                    id=uuid.uuid4(),
                    public_id=await self._next_public_id(session),
                    requested_by=requested_by,
                    source_connection_id=source_connection_id,
                    destination_connection_id=destination_connection_id,
                    source_paths=source_paths,
                    destination_path=destination_path,
                    operation="COPY",
                    conflict_policy=conflict_policy,
                    send_email_notification=send_email_notification,
                    status="QUEUED",
                    source_credential_version=by_id[source_connection_id].credential_version,
                    destination_credential_version=by_id[
                        destination_connection_id
                    ].credential_version,
                )
                session.add(row)
                await session.flush()
            await session.refresh(row)
            return self._transfer(row)

    async def aget_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        async with self.sessions() as session:
            row = await session.get(TransferJob, transfer_id)
            return self._transfer(row) if row else None

    async def alist_transfers(self, requested_by: uuid.UUID | None = None) -> list[TransferRecord]:
        async with self.sessions() as session:
            statement = select(TransferJob).order_by(TransferJob.created_at.desc())
            if requested_by is not None:
                statement = statement.where(TransferJob.requested_by == requested_by)
            rows = (await session.scalars(statement)).all()
            return [self._transfer(row) for row in rows]

    async def asave_transfer(self, transfer: TransferRecord) -> None:
        async with self.sessions() as session:
            row = await session.get(TransferJob, transfer.id)
            if row is None:
                return
            row.status = transfer.status
            row.progress_percent = transfer.progress_percent
            row.files_total = transfer.files_total
            row.files_completed = transfer.files_completed
            row.bytes_total = transfer.bytes_total
            row.bytes_transferred = transfer.bytes_transferred
            row.current_item = transfer.current_item
            row.average_speed_bps = transfer.average_speed_bps
            row.current_speed_bps = transfer.current_speed_bps
            row.estimated_remaining_seconds = transfer.estimated_remaining_seconds
            row.attempt_count = transfer.attempt_count
            row.error_count = transfer.error_count
            row.warning_count = transfer.warning_count
            row.started_at = transfer.started_at
            row.completed_at = transfer.completed_at
            await session.commit()

    async def aadd_transfer_manifest_items(
        self, transfer_id: uuid.UUID, entries: list[dict[str, Any]]
    ) -> int:
        if not entries:
            return 0
        async with self.sessions() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            inserted = 0
            async with session.begin():
                for start in range(0, len(entries), 200):
                    values = []
                    for entry in entries[start : start + 200]:
                        source_path = str(entry["source_path"])
                        is_dir = bool(entry.get("is_dir", False))
                        values.append(
                            {
                                "id": uuid.uuid4(),
                                "transfer_job_id": transfer_id,
                                "source_path": source_path,
                                "destination_path": str(entry["destination_path"]),
                                "source_path_digest": hashlib.sha256(
                                    f"{transfer_id}:{source_path}".encode()
                                ).hexdigest(),
                                "is_dir": is_dir,
                                "size_bytes": entry.get("size_bytes"),
                                "modified_at": entry.get("modified_at"),
                                "scan_complete": False,
                                "status": "DIRECTORY" if is_dir else "PENDING",
                                "copy_started": False,
                                "destination_existed_before": False,
                            }
                        )
                    statement = insert(TransferManifestItem).values(values)
                    result = await session.execute(
                        statement.on_conflict_do_nothing(
                            index_elements=[
                                TransferManifestItem.transfer_job_id,
                                TransferManifestItem.source_path_digest,
                            ]
                        ).returning(TransferManifestItem.id)
                    )
                    inserted += len(result.scalars().all())
                transfer = await session.get(TransferJob, transfer_id)
                if transfer is None:
                    raise ValueError("Transfer not found")
                transfer.files_total = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TransferManifestItem)
                        .where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.is_dir.is_(False),
                        )
                    )
                    or 0
                )
                transfer.bytes_total = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(TransferManifestItem.size_bytes), 0)).where(
                            TransferManifestItem.transfer_job_id == transfer_id,
                            TransferManifestItem.is_dir.is_(False),
                        )
                    )
                    or 0
                )
            return inserted

    async def alist_pending_scan_directories(
        self, transfer_id: uuid.UUID, limit: int = 200
    ) -> list[TransferManifestItemRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.is_dir.is_(True),
                        TransferManifestItem.scan_complete.is_(False),
                    )
                    .order_by(TransferManifestItem.id)
                    .limit(max(1, min(limit, 1000)))
                )
            ).all()
            return [self._manifest_item(row) for row in rows]

    async def amark_transfer_directory_scanned(
        self, transfer_id: uuid.UUID, item_id: uuid.UUID
    ) -> None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferManifestItem).where(
                    TransferManifestItem.transfer_job_id == transfer_id,
                    TransferManifestItem.id == item_id,
                    TransferManifestItem.is_dir.is_(True),
                )
            )
            if row is None:
                raise ValueError("Manifest directory not found")
            row.scan_complete = True
            await session.commit()

    async def amark_transfer_manifest_item_started(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        destination_existed_before: bool,
    ) -> None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TransferManifestItem)
                .where(
                    TransferManifestItem.transfer_job_id == transfer_id,
                    TransferManifestItem.id == item_id,
                    TransferManifestItem.is_dir.is_(False),
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Manifest file not found")
            if not row.copy_started:
                row.copy_started = True
                row.destination_existed_before = destination_existed_before
            await session.commit()

    async def alist_transfer_manifest_items(
        self,
        transfer_id: uuid.UUID,
        statuses: set[str] | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 200,
        include_directories: bool = False,
    ) -> list[TransferManifestItemRecord]:
        async with self.sessions() as session:
            statement = select(TransferManifestItem).where(
                TransferManifestItem.transfer_job_id == transfer_id,
            )
            if not include_directories:
                statement = statement.where(TransferManifestItem.is_dir.is_(False))
            if statuses:
                statement = statement.where(TransferManifestItem.status.in_(statuses))
            if after_id is not None:
                statement = statement.where(TransferManifestItem.id > after_id)
            rows = (
                await session.scalars(
                    statement.order_by(TransferManifestItem.id).limit(max(1, min(limit, 1000)))
                )
            ).all()
            return [self._manifest_item(row) for row in rows]

    async def aupdate_transfer_manifest_item(
        self,
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        status: str,
        verified_by: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"COPIED", "SKIPPED", "FAILED", "PENDING"}:
            raise ValueError("Invalid manifest result")
        async with self.sessions() as session:
            async with session.begin():
                transfer = await session.scalar(
                    select(TransferJob).where(TransferJob.id == transfer_id).with_for_update()
                )
                item = await session.scalar(
                    select(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.id == item_id,
                        TransferManifestItem.is_dir.is_(False),
                    )
                    .with_for_update()
                )
                if transfer is None or item is None:
                    raise ValueError("Manifest file not found")
                old_status = item.status
                if (
                    old_status == status
                    and item.verified_by == verified_by
                    and item.error_code == error_code
                ):
                    return
                terminal = {"COPIED", "SKIPPED", "FAILED"}
                if old_status not in terminal and status in terminal:
                    transfer.files_completed += 1
                elif old_status in terminal and status not in terminal:
                    transfer.files_completed = max(0, transfer.files_completed - 1)
                was_copied = old_status == "COPIED"
                becomes_copied = status == "COPIED"
                if was_copied != becomes_copied:
                    transfer.bytes_transferred = max(
                        0,
                        transfer.bytes_transferred
                        + ((item.size_bytes or 0) if becomes_copied else -(item.size_bytes or 0)),
                    )
                if old_status == "FAILED":
                    transfer.error_count = max(0, transfer.error_count - 1)
                if old_status in {"FAILED", "SKIPPED"}:
                    transfer.warning_count = max(0, transfer.warning_count - 1)
                if status == "FAILED":
                    transfer.error_count += 1
                if status in {"FAILED", "SKIPPED"}:
                    transfer.warning_count += 1
                item.status = status
                item.verified_by = verified_by
                item.error_code = error_code
                if transfer.bytes_total:
                    transfer.progress_percent = min(
                        100.0,
                        round(transfer.bytes_transferred * 100 / transfer.bytes_total, 2),
                    )
                elif transfer.files_total:
                    transfer.progress_percent = min(
                        100.0,
                        round(transfer.files_completed * 100 / transfer.files_total, 2),
                    )

    async def aget_transfer_manifest_counts(self, transfer_id: uuid.UUID) -> dict[str, int]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(TransferManifestItem.status, func.count())
                    .where(TransferManifestItem.transfer_job_id == transfer_id)
                    .group_by(TransferManifestItem.status)
                )
            ).all()
            counts = {status: int(count) for status, count in rows}
            pending_directories = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TransferManifestItem)
                    .where(
                        TransferManifestItem.transfer_job_id == transfer_id,
                        TransferManifestItem.is_dir.is_(True),
                        TransferManifestItem.scan_complete.is_(False),
                    )
                )
                or 0
            )
            return {
                "total": sum(counts.values()),
                "pending": counts.get("PENDING", 0) + pending_directories,
                "copied": counts.get("COPIED", 0),
                "skipped": counts.get("SKIPPED", 0),
                "failed": counts.get("FAILED", 0),
            }

    async def aadd_transfer_event(
        self,
        transfer_job_id: uuid.UUID,
        *,
        level: str,
        event_type: str,
        message: str,
        technical_details: dict[str, Any] | None = None,
        provider_error_code: str | None = None,
        item_path: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        async with self.sessions() as session:
            session.add(
                TransferEvent(
                    transfer_job_id=transfer_job_id,
                    level=level,
                    event_type=event_type[:64],
                    message=message[:2000],
                    technical_details=sanitize_data(technical_details),
                    provider_error_code=provider_error_code,
                    item_path=item_path[:1024] if item_path else None,
                    correlation_id=correlation_id or transfer_job_id,
                )
            )
            await session.commit()

    async def alist_transfer_events(self, transfer_job_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(TransferEvent)
                    .where(TransferEvent.transfer_job_id == transfer_job_id)
                    .order_by(TransferEvent.created_at)
                )
            ).all()
            return [
                {
                    "id": str(row.id),
                    "level": row.level,
                    "event_type": row.event_type,
                    "message": row.message,
                    "technical_details": row.technical_details,
                    "provider_error_code": row.provider_error_code,
                    "item_path": row.item_path,
                    "correlation_id": str(row.correlation_id),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    async def aadd_audit_event(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        result: str,
        resource_id: str | None = None,
        metadata: dict[str, Any],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    action=action[:64],
                    resource_type=resource_type[:64],
                    resource_id=resource_id[:64] if resource_id else None,
                    result=result,
                    event_metadata=sanitize_data(metadata),
                    ip_address=ip_address,
                    user_agent=user_agent[:255] if user_agent else None,
                )
            )
            await session.commit()

    async def alist_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
                )
            ).all()
            return [
                {
                    "id": str(row.id),
                    "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "result": row.result,
                    "metadata": row.event_metadata,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
