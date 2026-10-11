import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.security.sanitize import sanitize_data

ACTIVE_TRANSFER_STATUSES = frozenset(
    {"QUEUED", "VALIDATING", "RUNNING", "RETRYING", "CANCELLING"}
)


class ConnectionInUseError(ValueError):
    """Raised when a connection has an active transfer and cannot be archived."""


class ConnectionUnavailableError(ValueError):
    """Raised when a connection is missing, archived, or inactive."""

    def __init__(self, reason: Literal["not_found", "inactive"]) -> None:
        self.reason = reason
        message = "Connection not found" if reason == "not_found" else "Connection is inactive"
        super().__init__(message)


@dataclass
class UserAccount:
    id: uuid.UUID
    username: str
    email: str
    password_hash: str
    role: str
    is_active: bool = True
    must_change_password: bool = False
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None


@dataclass
class ConnectionRecord:
    id: uuid.UUID
    name: str
    provider_type: str
    created_by: uuid.UUID
    encrypted_credentials: bytes = b""
    credentials_configured: bool = False
    status: str = "ACTIVE"
    last_tested_at: datetime | None = None
    last_test_status: str | None = None
    is_global: bool = True
    deleted_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "provider_type": self.provider_type,
            "is_global": self.is_global,
            "status": self.status,
            "credentials_configured": bool(self.encrypted_credentials),
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "last_test_status": self.last_test_status,
        }


@dataclass
class TransferRecord:
    id: uuid.UUID
    public_id: str
    requested_by: uuid.UUID
    source_connection_id: uuid.UUID
    destination_connection_id: uuid.UUID
    source_paths: list[str]
    destination_path: str
    conflict_policy: str = "OVERWRITE_IF_DIFFERENT"
    send_email_notification: bool = False
    status: str = "QUEUED"
    progress_percent: float = 0.0
    files_total: int = 0
    files_completed: int = 0
    bytes_total: int = 0
    bytes_transferred: int = 0
    current_item: str | None = None
    average_speed_bps: int | None = None
    current_speed_bps: int | None = None
    estimated_remaining_seconds: int | None = None
    attempt_count: int = 1
    error_count: int = 0
    warning_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "public_id": self.public_id,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "files_total": self.files_total,
            "files_completed": self.files_completed,
            "bytes_total": self.bytes_total,
            "bytes_transferred": self.bytes_transferred,
            "current_item": self.current_item,
            "average_speed_bps": self.average_speed_bps,
            "current_speed_bps": self.current_speed_bps,
            "estimated_remaining_seconds": self.estimated_remaining_seconds,
            "attempt_count": self.attempt_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "source_connection_id": str(self.source_connection_id),
            "destination_connection_id": str(self.destination_connection_id),
            "source_paths": self.source_paths,
            "destination_path": self.destination_path,
            "conflict_policy": self.conflict_policy,
            "send_email_notification": self.send_email_notification,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class MemoryStore:
    """In-memory repository for the mock vertical slice and isolated tests."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, UserAccount] = {}
        self.connections: dict[uuid.UUID, ConnectionRecord] = {}
        self.transfers: dict[uuid.UUID, TransferRecord] = {}
        self.connection_grants: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.transfer_events: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.audit_events: list[dict[str, Any]] = []

    async def acheck(self) -> bool:
        return True

    def add_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        user = UserAccount(
            id=uuid.uuid4(),
            username=username,
            email=email,
            password_hash=password_hash,
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        self.users[user.id] = user
        return user

    def find_user(self, username_or_email: str) -> UserAccount | None:
        normalized = username_or_email.casefold()
        return next(
            (
                user
                for user in self.users.values()
                if user.username.casefold() == normalized or user.email.casefold() == normalized
            ),
            None,
        )

    def get_user(self, user_id: uuid.UUID) -> UserAccount | None:
        return self.users.get(user_id)

    def create_connection(
        self,
        name: str,
        provider_type: str,
        created_by: uuid.UUID,
        connection_id: uuid.UUID | None = None,
        encrypted_credentials: bytes = b"",
        is_global: bool = True,
    ) -> ConnectionRecord:
        record = ConnectionRecord(
            id=connection_id or uuid.uuid4(),
            name=name,
            provider_type=provider_type,
            created_by=created_by,
            encrypted_credentials=encrypted_credentials,
            is_global=is_global,
        )
        self.connections[record.id] = record
        return record

    def get_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        connection = self.connections.get(connection_id)
        return connection if connection and connection.deleted_at is None else None

    def list_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        connections: Iterable[ConnectionRecord] = (
            connection
            for connection in self.connections.values()
            if connection.deleted_at is None
        )
        if user_id is not None:
            connections = (
                connection
                for connection in connections
                if connection.is_global or (user_id, connection.id) in self.connection_grants
            )
        return sorted(connections, key=lambda record: record.name.casefold())

    def next_public_id(self, now: datetime | None = None) -> str:
        year = (now or datetime.now(UTC)).year
        count = sum(
            1
            for transfer in self.transfers.values()
            if transfer.public_id.startswith(f"TRF-{year}-")
        )
        return f"TRF-{year}-{count + 1:06d}"

    def create_transfer(
        self,
        requested_by: uuid.UUID,
        source_connection_id: uuid.UUID,
        destination_connection_id: uuid.UUID,
        source_paths: list[str],
        destination_path: str,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
        send_email_notification: bool = False,
    ) -> TransferRecord:
        record = TransferRecord(
            id=uuid.uuid4(),
            public_id=self.next_public_id(),
            requested_by=requested_by,
            source_connection_id=source_connection_id,
            destination_connection_id=destination_connection_id,
            source_paths=source_paths,
            destination_path=destination_path,
            conflict_policy=conflict_policy,
            send_email_notification=send_email_notification,
        )
        self.transfers[record.id] = record
        return record

    def get_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        return self.transfers.get(transfer_id)

    def list_transfers(self, requested_by: uuid.UUID | None = None) -> list[TransferRecord]:
        records = [
            record
            for record in self.transfers.values()
            if requested_by is None or record.requested_by == requested_by
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    async def aget_user(self, user_id: uuid.UUID) -> UserAccount | None:
        return self.get_user(user_id)

    async def alist_users(self) -> list[UserAccount]:
        return sorted(self.users.values(), key=lambda user: user.username.casefold())

    async def acount_active_admins(self) -> int:
        return sum(1 for user in self.users.values() if user.is_active and user.role == "ADMIN")

    async def acreate_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> UserAccount:
        if self.find_user(username) or self.find_user(email):
            raise ValueError("Username or email is already in use")
        return self.add_user(username, email, password_hash, role, is_active, must_change_password)

    async def afind_user(self, username_or_email: str) -> UserAccount | None:
        return self.find_user(username_or_email)

    async def asave_user(self, user: UserAccount) -> None:
        existing_username = self.find_user(user.username)
        existing_email = self.find_user(user.email)
        if any(
            existing is not None and existing.id != user.id
            for existing in (existing_username, existing_email)
        ):
            raise ValueError("Username or email is already in use")
        self.users[user.id] = user

    async def aget_connection(self, connection_id: uuid.UUID) -> ConnectionRecord | None:
        return self.get_connection(connection_id)

    async def aarchive_connection(
        self,
        connection_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectionRecord | None:
        connection = self.connections.get(connection_id)
        if connection is None or connection.deleted_at is not None:
            return None
        if any(
            transfer.status in ACTIVE_TRANSFER_STATUSES
            and connection_id
            in {transfer.source_connection_id, transfer.destination_connection_id}
            for transfer in self.transfers.values()
        ):
            raise ConnectionInUseError("Connection has an active transfer")
        connection.deleted_at = datetime.now(UTC)
        connection.encrypted_credentials = b""
        self.connection_grants = {
            grant for grant in self.connection_grants if grant[1] != connection_id
        }
        self.audit_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": str(actor_user_id),
                "action": "CONNECTION_ARCHIVED",
                "resource_type": "connection",
                "resource_id": str(connection.id),
                "result": "SUCCESS",
                "metadata": sanitize_data(
                    {"provider_type": connection.provider_type, "is_global": connection.is_global}
                ),
                "ip_address": ip_address,
                "user_agent": user_agent[:255] if user_agent else None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return connection

    async def alist_connections(self, user_id: uuid.UUID | None = None) -> list[ConnectionRecord]:
        return self.list_connections(user_id)

    async def ahas_connection_access(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        connection = self.get_connection(connection_id)
        return bool(
            connection
            and (connection.is_global or (user_id, connection_id) in self.connection_grants)
        )

    async def agrant_connection(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, granted_by: uuid.UUID
    ) -> None:
        del granted_by
        if self.get_connection(connection_id) is None:
            raise ConnectionUnavailableError("not_found")
        self.connection_grants.add((user_id, connection_id))

    async def arevoke_connection(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self.connection_grants.discard((user_id, connection_id))

    async def acreate_connection(
        self,
        name: str,
        provider_type: str,
        created_by: uuid.UUID,
        connection_id: uuid.UUID,
        encrypted_credentials: bytes,
        is_global: bool = True,
    ) -> ConnectionRecord:
        return self.create_connection(
            name, provider_type, created_by, connection_id, encrypted_credentials, is_global
        )

    async def asave_connection(self, connection: ConnectionRecord) -> None:
        current = self.connections.get(connection.id)
        if current is None or current.deleted_at is not None:
            raise ConnectionUnavailableError("not_found")
        connection.deleted_at = current.deleted_at
        self.connections[connection.id] = connection

    async def aget_transfer(self, transfer_id: uuid.UUID) -> TransferRecord | None:
        return self.get_transfer(transfer_id)

    async def alist_transfers(self, requested_by: uuid.UUID | None = None) -> list[TransferRecord]:
        return self.list_transfers(requested_by)

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
        for connection_id in {source_connection_id, destination_connection_id}:
            connection = self.get_connection(connection_id)
            if connection is None:
                raise ConnectionUnavailableError("not_found")
            if connection.status != "ACTIVE":
                raise ConnectionUnavailableError("inactive")
        return self.create_transfer(
            requested_by,
            source_connection_id,
            destination_connection_id,
            source_paths,
            destination_path,
            conflict_policy,
            send_email_notification,
        )

    async def asave_transfer(self, transfer: TransferRecord) -> None:
        self.transfers[transfer.id] = transfer

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
        self.transfer_events.setdefault(transfer_job_id, []).append(
            {
                "level": level,
                "event_type": event_type,
                "message": message,
                "technical_details": sanitize_data(technical_details),
                "provider_error_code": provider_error_code,
                "item_path": item_path,
                "correlation_id": str(correlation_id or transfer_job_id),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def alist_transfer_events(self, transfer_job_id: uuid.UUID) -> list[dict[str, Any]]:
        return list(self.transfer_events.get(transfer_job_id, []))

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
        self.audit_events.append(
            {
                "id": str(uuid.uuid4()),
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "result": result,
                "metadata": sanitize_data(metadata),
                "ip_address": ip_address,
                "user_agent": user_agent,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    async def alist_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(reversed(self.audit_events[-limit:]))
