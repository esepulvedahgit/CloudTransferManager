import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from app.providers.base import ConnectionTestResult
from app.providers.megacmd import MegaCmdProvider
from app.providers.mock import MockStorageProvider
from app.providers.nextcloud import NextcloudRcloneProvider
from app.providers.rclone import RcloneGoogleDriveProvider
from app.providers.seafile_auth import (
    SeafileAuthClient,
    SeafileAuthenticationError,
    normalize_seafile_base_url,
)
from app.providers.smb import RcloneSmbProvider
from app.security.credentials import CredentialCipher
from app.security.passwords import PasswordService
from app.services.audit import AuditLogger
from app.services.database_store import DatabaseStore
from app.services.event_bus import RedisEventBus
from app.services.megacmd import MegaCmdRunner
from app.services.memory_store import (
    ConnectionInUseError,
    ConnectionRecord,
    ConnectionUnavailableError,
    IdempotencyConflictError,
    MemoryStore,
    SMTPSettingsRecord,
    TransferRecord,
    UserAccount,
)
from app.services.rclone import RcloneRunner
from app.services.smtp_config import (
    SMTPConnectionError,
    SMTPServerConfig,
    decrypt_smtp_credentials,
    encrypt_smtp_credentials,
    environment_smtp_config,
    test_smtp_connection,
    validate_smtp_host,
    validate_smtp_sender,
)
from app.services.transfer_evaluation import EvaluationConflictError
from app.services.transfer_executor import TransferExecutor
from app.services.transfer_omission_report import iter_omissions_csv
from app.services.transfer_omissions import (
    SourceFileErrorPolicy,
    TransferRecoveryConflictError,
    TransferRecoveryNotFoundError,
    resolve_failure_evidence,
    skip_unavailable_reason,
)
from app.services.transfer_recovery import (
    AcceptParentOmissionsRequest,
    ResumeTransferRequest,
    RetryOmittedRequest,
    SkipAndResumeRequest,
    accept_parent_omissions,
    retry_omitted_transfer_items,
    skip_and_resume_transfer,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return PasswordService().hash(secrets.token_urlsafe(32))


def encode_transfer_incident_cursor(occurred_at: datetime, item_id: uuid.UUID) -> str:
    payload = f"{occurred_at.astimezone(UTC).isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_transfer_incident_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        timestamp, item_id = decoded.split("|", maxsplit=1)
        occurred_at = datetime.fromisoformat(timestamp)
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Cursor timestamp must include a timezone")
        return occurred_at.astimezone(UTC), uuid.UUID(item_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Cursor de incidencias inválido") from exc


class LoginRequest(BaseModel):
    username_or_email: str
    password: str


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern="^[A-Za-z0-9_.-]+$")
    email: str = Field(min_length=3, max_length=255, pattern="^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
    password: str = Field(min_length=12, max_length=128)
    role: str = Field(default="USER", pattern="^(ADMIN|USER)$")
    must_change_password: bool = True


class UserUpdate(BaseModel):
    username: str | None = Field(
        default=None, min_length=1, max_length=64, pattern="^[A-Za-z0-9_.-]+$"
    )
    email: str | None = Field(
        default=None,
        min_length=3,
        max_length=255,
        pattern="^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$",
    )
    role: str | None = Field(default=None, pattern="^(ADMIN|USER)$")
    is_active: bool | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)


class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=128)


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider_type: str = Field(pattern="^(MOCK|MEGA|GOOGLE_DRIVE|SEAFILE|SMB|NEXTCLOUD)$")
    adapter_type: str = Field(default="RCLONE", pattern="^(RCLONE|SEAFILE_API)$")
    credentials: dict[str, str] | None = None
    is_global: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_global: bool | None = None
    adapter_type: str | None = Field(default=None, pattern="^(RCLONE|SEAFILE_API)$")
    credentials: dict[str, str] | None = None


class ConnectionTestRequest(BaseModel):
    otp: str | None = Field(default=None, min_length=1, max_length=32)


class SMTPSettingsRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    use_tls: bool
    from_address: str = Field(min_length=3, max_length=255)
    username: SecretStr | None = Field(default=None, max_length=255)
    password: SecretStr | None = Field(default=None, max_length=1024)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return validate_smtp_host(value)

    @field_validator("from_address")
    @classmethod
    def validate_from_address(cls, value: str) -> str:
        return validate_smtp_sender(value)

    @model_validator(mode="after")
    def validate_credential_pair(self) -> "SMTPSettingsRequest":
        username = self.username.get_secret_value().strip() if self.username else ""
        password = self.password.get_secret_value() if self.password else ""
        if bool(username) != bool(password):
            raise ValueError("SMTP username and password must be supplied together")
        for value in (username, password):
            if any(character in value for character in "\r\n\x00"):
                raise ValueError("Invalid SMTP credential")
        return self


_GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _google_callback_url(app_base_url: str, app_env: str) -> str | None:
    parsed = urlsplit(app_base_url.strip())
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (app_env.lower() == "production" and parsed.scheme != "https")
    ):
        return None
    return f"{app_base_url.rstrip('/')}/api/v1/connections/google-oauth/callback"


class TransferCreate(BaseModel):
    source_connection_id: uuid.UUID
    destination_connection_id: uuid.UUID
    source_paths: list[str] = Field(min_length=1)
    source_item_ids: list[str | None] | None = None
    source_parent_ids: list[str | None] | None = None
    destination_path: str = Field(min_length=1, max_length=1024)
    operation: str = "COPY"
    conflict_policy: str = "KEEP_BOTH"
    send_email_notification: bool = False
    scheduled_at: datetime | None = None
    schedule_timezone: str = "UTC"
    execution_mode: Literal["AUTO", "EXCLUSIVE"] = "AUTO"
    depends_on_transfer_id: uuid.UUID | None = None
    source_file_error_policy: SourceFileErrorPolicy = SourceFileErrorPolicy.STOP

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("La fecha programada debe incluir una zona horaria.")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("La fecha programada debe ser futura.")
        return normalized

    @field_validator("schedule_timezone")
    @classmethod
    def validate_schedule_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Selecciona una zona horaria válida.") from exc
        return value

    @field_validator("source_paths")
    @classmethod
    def validate_source_path_limit(cls, value: list[str]) -> list[str]:
        if len(value) > 100:
            raise ValueError("Selecciona como máximo 100 rutas por transferencia.")
        return value

    @model_validator(mode="after")
    def validate_source_item_ids(self) -> "TransferCreate":
        for values in (self.source_item_ids, self.source_parent_ids):
            if values is None:
                continue
            if len(values) != len(self.source_paths):
                raise ValueError("Cada ruta de origen debe corresponder a sus identificadores.")
            if any(
                value is not None
                and (not value or len(value) > 512 or any(ord(char) < 32 for char in value))
                for value in values
            ):
                raise ValueError("Identificador de elemento de origen no válido.")
        return self


class TransferScheduleUpdate(BaseModel):
    scheduled_at: datetime | None
    schedule_timezone: str
    execution_mode: Literal["AUTO", "EXCLUSIVE"]
    depends_on_transfer_id: uuid.UUID | None = None

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("La fecha programada debe incluir una zona horaria.")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("La fecha programada debe ser futura.")
        return normalized

    @field_validator("schedule_timezone")
    @classmethod
    def validate_schedule_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Selecciona una zona horaria válida.") from exc
        return value


class EventHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, transfer_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[transfer_id].add(queue)
        return queue

    def unsubscribe(self, transfer_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers[transfer_id].discard(queue)

    async def publish(self, transfer_id: str, event: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers.get(transfer_id, ())):
            queue.put_nowait(event)


def create_app(settings: Settings | None = None, store: Any | None = None) -> FastAPI:
    config = settings or get_settings()
    configured_secret = config.session_secret()
    if config.app_env.lower() == "production" and not configured_secret:
        raise RuntimeError("APP_SECRET_KEY must be configured in production")
    session_secret = configured_secret or secrets.token_urlsafe(48)
    app = FastAPI(title="Cloud Transfer Manager", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path in {
            "/api/v1/settings/smtp",
            "/api/v1/settings/smtp/test",
        }:
            return JSONResponse(
                status_code=422,
                content={"detail": "Configuración SMTP inválida"},
            )
        return await request_validation_exception_handler(request, exc)

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="ctm_session",
        max_age=config.session_max_age_seconds,
        same_site="lax",
        https_only=config.session_cookie_secure,
    )
    app.state.store = store or (
        DatabaseStore(config) if config.app_env.lower() == "production" else MemoryStore()
    )
    app.state.event_hub = EventHub()
    app.state.redis_client = (
        Redis.from_url(config.redis_url, decode_responses=True)
        if config.app_env.lower() == "production"
        else None
    )
    app.state.event_bus = RedisEventBus(app.state.redis_client) if app.state.redis_client else None
    app.state.tasks = set()
    app.state.cancel_requests = set()
    app.state.metrics_cache = {}
    app.state.metrics_cache_lock = asyncio.Lock()
    app.state.password_service = PasswordService()
    app.state.dummy_password_hash = _dummy_password_hash()
    app.state.audit_logger = AuditLogger(app.state.store)

    @app.middleware("http")
    async def audit_denied_api_access(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/") and response.status_code in {401, 403}:
            try:
                await app.state.audit_logger.record(
                    actor_user_id=None,
                    action="ACCESS_DENIED",
                    resource_type="api",
                    result="FAILURE",
                    metadata={"path": request.url.path, "status_code": response.status_code},
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            except Exception:
                pass
        return cast(Response, response)

    async def csrf_guard(
        request: Request,
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> None:
        expected = request.session.get("csrf_token")
        cookie = request.cookies.get("ctm_csrf")
        if not expected or not cookie or not x_csrf_token:
            raise HTTPException(status_code=403, detail="Token CSRF inválido")
        if not (
            hmac.compare_digest(expected, cookie) and hmac.compare_digest(expected, x_csrf_token)
        ):
            raise HTTPException(status_code=403, detail="Token CSRF inválido")

    async def current_user(request: Request) -> UserAccount:
        raw_id = request.session.get("user_id")
        try:
            user_id = uuid.UUID(raw_id)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=401, detail="Autenticación requerida") from None
        user = await app.state.store.aget_user(user_id)
        if user is None or not user.is_active:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Autenticación requerida")
        if user.must_change_password and request.url.path not in {
            "/api/v1/auth/me",
            "/api/v1/auth/password",
            "/api/v1/auth/logout",
        }:
            raise HTTPException(
                status_code=403,
                detail="Debes cambiar tu contraseña antes de usar la aplicación",
            )
        return cast(UserAccount, user)

    async def admin_user(user: UserAccount = Depends(current_user)) -> UserAccount:
        if user.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Permisos insuficientes")
        return user

    def credentials_for(connection: ConnectionRecord) -> dict[str, Any]:
        if not connection.encrypted_credentials:
            raise HTTPException(
                status_code=422, detail="La conexión no tiene credenciales configuradas"
            )
        try:
            return CredentialCipher.from_secret_file(config.master_encryption_key_file).decrypt(
                connection.id, connection.encrypted_credentials
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail="No se pudieron cargar las credenciales"
            ) from exc

    def provider_for(
        connection: ConnectionRecord,
        credentials_override: dict[str, Any] | None = None,
    ) -> Any:
        if connection.provider_type == "MOCK":
            return MockStorageProvider()
        credentials = credentials_override or credentials_for(connection)
        provider_type = connection.provider_type
        if provider_type == "MEGA":
            return MegaCmdProvider(
                connection.id,
                credentials,
                MegaCmdRunner(config.megacmd_binary_dir),
                Path(config.transfer_staging_path),
            )
        if provider_type == "GOOGLE_DRIVE":
            return RcloneGoogleDriveProvider(
                connection.id,
                credentials,
                RcloneRunner(config.rclone_binary, config.transfer_temp_path),
            )
        if provider_type == "NEXTCLOUD":
            return NextcloudRcloneProvider(
                connection.id,
                credentials,
                RcloneRunner(
                    config.rclone_binary,
                    config.transfer_temp_path,
                    config.transfer_staging_path,
                ),
                credential_version=connection.credential_version,
                chunk_size=config.nextcloud_chunk_size,
            )
        if provider_type == "SMB":
            allowed_targets = {
                target.strip().casefold().rstrip(".")
                for target in config.smb_allowed_targets.split(",")
                if target.strip()
            }
            return RcloneSmbProvider(
                connection.id,
                credentials,
                RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                allowed_targets=allowed_targets,
                interactive_timeout_seconds=config.smb_interactive_timeout_seconds,
            )
        if provider_type == "SEAFILE":
            runner = RcloneRunner(
                config.rclone_binary,
                config.transfer_temp_path,
                config.transfer_staging_path,
            )
            if connection.adapter_type == "SEAFILE_API":
                provider_class = globals().get("SeafileApiProvider")
                if provider_class is None:
                    from app.providers.seafile_api import SeafileApiProvider

                    provider_class = SeafileApiProvider
                return provider_class(connection.id, credentials)
            provider_class = globals().get("SeafileRcloneProvider")
            if provider_class is None:
                from app.providers.seafile import SeafileRcloneProvider

                provider_class = SeafileRcloneProvider
            return provider_class(connection.id, credentials, runner)
        raise HTTPException(status_code=422, detail="Proveedor no soportado")

    def validate_seafile_credentials(credentials: dict[str, str]) -> dict[str, str]:
        allowed = {"url", "username", "password", "library", "library_password"}
        if set(credentials) - allowed:
            raise ValueError("Unsupported Seafile credential field")
        normalized = dict(credentials)
        for field_name in ("url", "username", "password"):
            value = normalized.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Seafile URL, username, and password are required")
            if any(character in value for character in "\r\n\x00"):
                raise ValueError("Invalid Seafile credential value")
        normalized["url"] = normalize_seafile_base_url(normalized["url"])
        if config.app_env.lower() == "production" and not normalized["url"].startswith("https://"):
            raise ValueError("Seafile connections in production require HTTPS")
        return normalized

    def transfer_event(record: TransferRecord) -> dict[str, Any]:
        data = record.public_dict()
        data.update(
            {
                "job_id": str(record.id),
                "current_speed_bps": record.current_speed_bps,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        record.events.append(data)
        return data

    async def attach_pause_capabilities(records: list[TransferRecord]) -> None:
        capabilities = await app.state.store.alist_transfer_pause_capabilities(
            [record.id for record in records]
        )
        for record in records:
            capability = capabilities.get(record.id)
            if capability is not None:
                record.pause_supported, record.pause_unavailable_reason = capability

    async def publish_transfer(record: TransferRecord) -> None:
        if record.pause_supported is None:
            await attach_pause_capabilities([record])
        record.manifest_summary = (
            await app.state.store.alist_transfer_manifest_summaries([record.id])
        )[record.id]
        await app.state.store.asave_transfer(record)
        event = transfer_event(record)
        try:
            await app.state.store.aadd_transfer_event(
                record.id,
                level="ERROR" if record.status == "FAILED" else "INFO",
                event_type=record.status,
                message=f"Estado de transferencia: {record.status}",
                technical_details={
                    "progress_percent": record.progress_percent,
                    "files_completed": record.files_completed,
                    "files_total": record.files_total,
                    "bytes_transferred": record.bytes_transferred,
                    "bytes_total": record.bytes_total,
                    "average_speed_bps": record.average_speed_bps,
                    "current_speed_bps": record.current_speed_bps,
                    "attempt_count": record.attempt_count,
                    "error_count": record.error_count,
                },
                provider_error_code="TRANSFER_FAILED" if record.status == "FAILED" else None,
            )
        except Exception as exc:
            logger.warning(
                "Transfer event persistence failed",
                extra={"transfer_id": str(record.id), "exception_type": type(exc).__name__},
            )
        try:
            if app.state.event_bus:
                await app.state.event_bus.publish_progress(str(record.id), event)
            else:
                await app.state.event_hub.publish(str(record.id), event)
        except Exception as exc:
            logger.warning(
                "Transfer progress publication failed",
                extra={"transfer_id": str(record.id), "exception_type": type(exc).__name__},
            )

    async def cancel_requested(transfer_id: uuid.UUID) -> bool:
        return bool(await app.state.store.ais_transfer_cancel_requested(transfer_id))

    async def audit_request(
        request: Request,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        result: str,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await app.state.audit_logger.record(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            metadata=metadata or {},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            await app.state.store.acheck()
            if app.state.event_bus:
                await app.state.event_bus.ping()
            if config.app_env.lower() == "production" and not (
                Path(config.rclone_binary).is_file() and os.access(config.rclone_binary, os.X_OK)
            ):
                raise RuntimeError("Rclone binary is not executable")
            if config.app_env.lower() == "production":
                mega_commands = (
                    "mega-cmd",
                    "mega-ls",
                    "mega-get",
                    "mega-put",
                    "mega-df",
                    "mega-logout",
                    "mega-quit",
                )
                if not all(
                    (Path(config.megacmd_binary_dir) / command).is_file()
                    and os.access(Path(config.megacmd_binary_dir) / command, os.X_OK)
                    for command in mega_commands
                ):
                    raise RuntimeError("MEGAcmd binaries are not executable")
            if config.app_env.lower() == "production":
                from app.worker import celery_app

                workers = await asyncio.to_thread(celery_app.control.ping, timeout=1)
                if not workers:
                    raise RuntimeError("No Celery workers are available")
                inspector = celery_app.control.inspect(timeout=1)
                active_queues = (
                    await asyncio.to_thread(inspector.active_queues) if inspector else None
                )
                available_queues = {
                    str(queue.get("name"))
                    for queues in (active_queues or {}).values()
                    for queue in queues
                    if isinstance(queue, dict) and queue.get("name")
                }
                if not {"transfers", "control"}.issubset(available_queues):
                    raise RuntimeError("Required Celery queues have no active consumers")
                if not await app.state.event_bus.beat_is_alive():
                    raise RuntimeError("Celery Beat heartbeat is stale")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Dependencias no disponibles") from exc
        return {"status": "ready"}

    @app.get("/health/notifications")
    async def notifications_health() -> dict[str, str]:
        if config.app_env.lower() != "production":
            return {"status": "not_required"}
        try:
            from app.worker import celery_app

            inspector = celery_app.control.inspect(timeout=1)
            active_queues = await asyncio.to_thread(inspector.active_queues) if inspector else None
            notification_queue_ready = any(
                isinstance(queue, dict) and queue.get("name") == "notifications"
                for queues in (active_queues or {}).values()
                for queue in queues
            )
            if not notification_queue_ready:
                raise RuntimeError("Notification queue has no active consumer")
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Servicio de notificaciones no disponible"
            ) from exc
        return {"status": "ready"}

    @app.get("/health/evaluations")
    async def evaluations_health() -> dict[str, str]:
        if config.app_env.lower() != "production":
            return {"status": "not_required"}
        try:
            from app.worker import celery_app

            inspector = celery_app.control.inspect(timeout=1)
            active_queues = await asyncio.to_thread(inspector.active_queues) if inspector else None
            evaluation_queue_ready = any(
                isinstance(queue, dict) and queue.get("name") == "evaluations"
                for queues in (active_queues or {}).values()
                for queue in queues
            )
            if not evaluation_queue_ready:
                raise RuntimeError("Evaluation queue has no active consumer")
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Servicio de evaluación no disponible"
            ) from exc
        return {"status": "ready"}

    async def dashboard_system_snapshot() -> dict[str, Any]:
        if config.app_env.lower() != "production":
            return {
                "environment": "development",
                "status": "SIMULATED",
                "services": {
                    "database": "simulated",
                    "redis": "not_configured",
                    "workers": "not_configured",
                    "beat": "not_configured",
                },
                "queues": [],
                "notifications": {},
                "evaluations": {},
                "connections": [],
            }

        services: dict[str, str] = {}
        queues: list[dict[str, Any]] = []
        try:
            await app.state.store.acheck()
            services["database"] = "ready"
        except Exception:
            services["database"] = "unavailable"
        try:
            if app.state.event_bus is None or not await app.state.event_bus.ping():
                raise RuntimeError("Redis unavailable")
            services["redis"] = "ready"
        except Exception:
            services["redis"] = "unavailable"

        try:
            from app.worker import celery_app

            responses = await asyncio.to_thread(celery_app.control.ping, timeout=1)
            inspector = celery_app.control.inspect(timeout=1)
            active_queues = (
                await asyncio.to_thread(inspector.active_queues) if inspector is not None else None
            )
            queue_names = {
                str(queue.get("name"))
                for worker_queues in (active_queues or {}).values()
                for queue in worker_queues
                if isinstance(queue, dict) and queue.get("name")
            }
            services["workers"] = (
                "ready" if responses and {"transfers", "control"} <= queue_names else "degraded"
            )
            services["beat"] = (
                "ready" if await app.state.event_bus.beat_is_alive() else "unavailable"
            )
            for name in ("transfers", "control", "evaluations", "notifications"):
                queues.append(
                    {
                        "name": name,
                        "consumer_ready": name in queue_names,
                        "pending": (
                            int(await app.state.redis_client.llen(name))
                            if app.state.redis_client is not None
                            else None
                        ),
                    }
                )
        except Exception:
            services["workers"] = "unavailable"
            services["beat"] = "unavailable"

        summaries = await app.state.store.ametric_system_summary()
        unavailable = sum(value != "ready" for value in services.values())
        return {
            "environment": "production",
            "status": "READY" if unavailable == 0 else "DEGRADED",
            "services": services,
            "queues": queues,
            **summaries,
        }

    @app.get("/api/v1/metrics/dashboard")
    async def transfer_metrics_dashboard(
        request: Request,
        period: Literal["today", "7d", "30d", "90d", "custom"] = Query(
            default="30d", alias="range"
        ),
        date_from: date | None = Query(default=None),
        date_to: date | None = Query(default=None),
        timezone: str = Query(default="UTC", min_length=1, max_length=64),
        source_provider: Literal["MEGA", "GOOGLE_DRIVE", "SEAFILE", "SMB", "NEXTCLOUD", "MOCK"]
        | None = None,
        destination_provider: Literal["MEGA", "GOOGLE_DRIVE", "SEAFILE", "SMB", "NEXTCLOUD", "MOCK"]
        | None = None,
        source_connection_id: uuid.UUID | None = None,
        destination_connection_id: uuid.UUID | None = None,
        include_mock: bool = False,
        user_id: uuid.UUID | None = None,
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        if user.role != "ADMIN" and user_id is not None:
            raise HTTPException(status_code=403, detail="Permisos insuficientes")
        scoped_user_id = user_id if user.role == "ADMIN" else user.id
        if user_id is not None and await app.state.store.aget_user(user_id) is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        if user.role != "ADMIN":
            for connection_id in (source_connection_id, destination_connection_id):
                if connection_id is not None and not await app.state.store.ahas_connection_access(
                    user.id, connection_id
                ):
                    raise HTTPException(status_code=404, detail="Conexión no encontrada")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(status_code=422, detail="Zona horaria no válida") from exc

        local_today = datetime.now(zone).date()
        if date_from is not None or date_to is not None:
            if date_from is None or date_to is None:
                raise HTTPException(status_code=422, detail="Indica ambas fechas del rango")
            start_day, end_day = date_from, date_to
            period = "custom"
        elif period == "custom":
            raise HTTPException(status_code=422, detail="El rango personalizado requiere fechas")
        elif period == "today":
            start_day = end_day = local_today
        else:
            days = {"7d": 7, "30d": 30, "90d": 90}[period]
            start_day, end_day = local_today - timedelta(days=days - 1), local_today
        if start_day > end_day or (end_day - start_day).days >= 366:
            raise HTTPException(status_code=422, detail="El rango máximo es de 366 días")
        start_at = datetime.combine(start_day, datetime.min.time(), zone).astimezone(UTC)
        end_at = datetime.combine(
            end_day + timedelta(days=1), datetime.min.time(), zone
        ).astimezone(UTC)
        filter_values = {
            "range": period,
            "from": start_day.isoformat(),
            "to": end_day.isoformat(),
            "timezone": timezone,
            "source_provider": source_provider,
            "destination_provider": destination_provider,
            "source_connection_id": str(source_connection_id) if source_connection_id else None,
            "destination_connection_id": str(destination_connection_id)
            if destination_connection_id
            else None,
            "include_mock": include_mock,
            "user_id": str(scoped_user_id) if scoped_user_id else None,
        }
        cache_key = (str(user.id), user.role, *filter_values.values())
        now = datetime.now(UTC)
        async with app.state.metrics_cache_lock:
            cached = app.state.metrics_cache.get(cache_key)
            if cached is not None and (now - cached[0]).total_seconds() < 15:
                return cast(dict[str, Any], cached[1])

        payload = cast(
            dict[str, Any],
            await app.state.store.ametric_dashboard(
                requested_by=scoped_user_id,
                date_from=start_at,
                date_to=end_at,
                timezone=timezone,
                source_provider=source_provider,
                destination_provider=destination_provider,
                source_connection_id=source_connection_id,
                destination_connection_id=destination_connection_id,
                include_mock=include_mock,
            ),
        )
        payload.update(
            {
                "generated_at": now.isoformat(),
                "filters": filter_values,
                "system": await dashboard_system_snapshot() if user.role == "ADMIN" else None,
            }
        )
        async with app.state.metrics_cache_lock:
            if len(app.state.metrics_cache) >= 256:
                app.state.metrics_cache.clear()
            app.state.metrics_cache[cache_key] = (now, payload)
        return payload

    @app.get("/api/v1/auth/csrf")
    async def csrf_token(request: Request, response: Response) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
        response.set_cookie(
            "ctm_csrf",
            token,
            max_age=config.session_max_age_seconds,
            httponly=False,
            secure=config.session_cookie_secure,
            samesite="lax",
            path="/",
        )
        return {"csrf_token": token}

    @app.post("/api/v1/auth/login")
    async def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        user = await app.state.store.afind_user(payload.username_or_email)
        now = datetime.now(UTC)
        invalid = HTTPException(
            status_code=401,
            detail="No fue posible iniciar sesión. Revisa tus datos e inténtalo nuevamente.",
        )
        if user is None:
            app.state.password_service.verify(payload.password, app.state.dummy_password_hash)
            await audit_request(
                request,
                actor_user_id=None,
                action="LOGIN",
                resource_type="user",
                result="FAILURE",
            )
            raise invalid
        if user.locked_until and user.locked_until > now:
            app.state.password_service.verify(payload.password, user.password_hash)
            await audit_request(
                request,
                actor_user_id=user.id,
                action="LOGIN",
                resource_type="user",
                resource_id=str(user.id),
                result="FAILURE",
            )
            raise invalid
        valid_password = app.state.password_service.verify(payload.password, user.password_hash)
        if not user.is_active or not valid_password:
            await app.state.store.arecord_login_failure(user.id, user.password_hash, now)
            await audit_request(
                request,
                actor_user_id=user.id,
                action="LOGIN",
                resource_type="user",
                resource_id=str(user.id),
                result="FAILURE",
            )
            raise invalid
        updated_user = await app.state.store.arecord_login_success(user.id, user.password_hash, now)
        if updated_user is None:
            await audit_request(
                request,
                actor_user_id=user.id,
                action="LOGIN",
                resource_type="user",
                resource_id=str(user.id),
                result="FAILURE",
            )
            raise invalid
        user = updated_user
        await audit_request(
            request,
            actor_user_id=user.id,
            action="LOGIN",
            resource_type="user",
            resource_id=str(user.id),
            result="SUCCESS",
        )
        request.session.clear()
        csrf = secrets.token_urlsafe(32)
        request.session.update({"user_id": str(user.id), "csrf_token": csrf})
        response.set_cookie(
            "ctm_csrf",
            csrf,
            max_age=config.session_max_age_seconds,
            httponly=False,
            secure=config.session_cookie_secure,
            samesite="lax",
            path="/",
        )
        return {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    @app.post("/api/v1/auth/logout")
    async def logout(
        request: Request,
        user_csrf: None = Depends(csrf_guard),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, str]:
        del user_csrf
        await audit_request(
            request,
            actor_user_id=user.id,
            action="LOGOUT",
            resource_type="user",
            resource_id=str(user.id),
            result="SUCCESS",
        )
        request.session.clear()
        return {"message": "Sesión cerrada"}

    @app.get("/api/v1/auth/me")
    async def me(user: UserAccount = Depends(current_user)) -> dict[str, Any]:
        return {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    @app.post("/api/v1/auth/password")
    async def change_password(
        payload: PasswordChange,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        if not app.state.password_service.verify(payload.current_password, user.password_hash):
            await audit_request(
                request,
                actor_user_id=user.id,
                action="PASSWORD_CHANGE",
                resource_type="user",
                resource_id=str(user.id),
                result="FAILURE",
            )
            raise HTTPException(status_code=400, detail="La contraseña actual no es válida")
        if payload.current_password == payload.new_password:
            raise HTTPException(status_code=422, detail="La nueva contraseña debe ser diferente")
        updated_user = await app.state.store.aupdate_user_password(
            user.id,
            app.state.password_service.hash(payload.new_password),
            must_change_password=False,
            expected_password_hash=user.password_hash,
        )
        if updated_user is None:
            raise HTTPException(
                status_code=409, detail="La contraseña cambió; inicia sesión nuevamente"
            )
        await audit_request(
            request,
            actor_user_id=user.id,
            action="PASSWORD_CHANGE",
            resource_type="user",
            resource_id=str(user.id),
            result="SUCCESS",
        )
        return {"message": "Contraseña actualizada"}

    @app.get("/api/v1/users")
    async def list_users(
        offset: int = Query(default=0, ge=0),
        page_size: int = Query(default=50, ge=1, le=200),
        _admin: UserAccount = Depends(admin_user),
    ) -> dict[str, Any]:
        users = await app.state.store.alist_users()
        items = [
            {
                "id": str(user.id),
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
                "must_change_password": user.must_change_password,
                "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            }
            for user in users[offset : offset + page_size]
        ]
        return {"items": items, "total_count": len(users)}

    @app.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
    async def create_user(
        payload: UserCreate,
        request: Request,
        _admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        try:
            created = await app.state.store.acreate_user(
                username=payload.username.strip().casefold(),
                email=payload.email.strip().casefold(),
                password_hash=app.state.password_service.hash(payload.password),
                role=payload.role,
                must_change_password=payload.must_change_password,
            )
        except (ValueError, IntegrityError) as exc:
            raise HTTPException(status_code=409, detail="El usuario o correo ya existe") from exc
        await audit_request(
            request,
            actor_user_id=_admin.id,
            action="USER_CREATED",
            resource_type="user",
            resource_id=str(created.id),
            result="SUCCESS",
            metadata={"role": created.role},
        )
        return {
            "id": str(created.id),
            "username": created.username,
            "email": created.email,
            "role": created.role,
            "is_active": created.is_active,
            "must_change_password": created.must_change_password,
        }

    @app.post("/api/v1/users/{user_id}/deactivate")
    async def deactivate_user(
        user_id: uuid.UUID,
        request: Request,
        admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        if user_id == admin.id:
            raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta")
        target, blocked_last_admin = await app.state.store.aupdate_user_profile(
            user_id, {"is_active": False}
        )
        if blocked_last_admin:
            raise HTTPException(
                status_code=409, detail="No se puede desactivar al último administrador"
            )
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        await audit_request(
            request,
            actor_user_id=admin.id,
            action="USER_DEACTIVATED",
            resource_type="user",
            resource_id=str(target.id),
            result="SUCCESS",
        )
        return {"message": "Usuario desactivado"}

    @app.patch("/api/v1/users/{user_id}")
    async def update_user(
        user_id: uuid.UUID,
        payload: UserUpdate,
        request: Request,
        admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        if not payload.model_fields_set:
            raise HTTPException(status_code=422, detail="No hay cambios para aplicar")
        if admin.id == user_id and payload.is_active is False:
            raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta")
        changes: dict[str, str | bool] = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "username" in changes:
            changes["username"] = str(changes["username"]).strip().casefold()
        if "email" in changes:
            changes["email"] = str(changes["email"]).strip().casefold()
        try:
            target, blocked_last_admin = await app.state.store.aupdate_user_profile(
                user_id, changes
            )
        except (IntegrityError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="El usuario o correo ya existe") from exc
        if blocked_last_admin:
            raise HTTPException(
                status_code=409, detail="No se puede quitar al último administrador"
            )
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        await audit_request(
            request,
            actor_user_id=admin.id,
            action="USER_UPDATED",
            resource_type="user",
            resource_id=str(target.id),
            result="SUCCESS",
            metadata={"role": target.role, "is_active": target.is_active},
        )
        return {
            "id": str(target.id),
            "username": target.username,
            "email": target.email,
            "role": target.role,
            "is_active": target.is_active,
            "must_change_password": target.must_change_password,
        }

    @app.post("/api/v1/users/{user_id}/password-reset")
    async def reset_user_password(
        user_id: uuid.UUID,
        payload: PasswordReset,
        request: Request,
        _admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        target = await app.state.store.aupdate_user_password(
            user_id,
            app.state.password_service.hash(payload.password),
            must_change_password=True,
            reset_login_failures=True,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        await audit_request(
            request,
            actor_user_id=_admin.id,
            action="PASSWORD_RESET",
            resource_type="user",
            resource_id=str(target.id),
            result="SUCCESS",
        )
        return {"message": "Contraseña restablecida"}

    def smtp_credentials_for_record(record: SMTPSettingsRecord | None) -> tuple[str, str]:
        if record is not None and record.encrypted_credentials:
            try:
                return decrypt_smtp_credentials(config, record)
            except (OSError, ValueError):
                raise HTTPException(
                    status_code=503, detail="No se pudieron cargar las credenciales SMTP"
                ) from None
        environment = environment_smtp_config(config)
        return environment.username, environment.password

    def smtp_settings_response(record: SMTPSettingsRecord | None) -> dict[str, Any]:
        if record is None:
            environment = environment_smtp_config(config)
            return {
                "host": environment.host,
                "port": environment.port,
                "use_tls": environment.use_tls,
                "from_address": environment.from_address,
                "credentials_configured": bool(environment.username and environment.password),
            }
        return {
            "host": record.host,
            "port": record.port,
            "use_tls": record.use_tls,
            "from_address": record.from_address,
            "credentials_configured": bool(record.encrypted_credentials),
        }

    def smtp_request_credentials(payload: SMTPSettingsRequest) -> tuple[str, str] | None:
        username = payload.username.get_secret_value().strip() if payload.username else ""
        password = payload.password.get_secret_value() if payload.password else ""
        return (username, password) if username and password else None

    def encrypt_smtp_values(settings_id: uuid.UUID, credentials: tuple[str, str]) -> bytes:
        try:
            return encrypt_smtp_credentials(config, settings_id, credentials[0], credentials[1])
        except (OSError, ValueError):
            raise HTTPException(
                status_code=503, detail="No se pudieron guardar las credenciales SMTP"
            ) from None

    @app.get("/api/v1/settings/smtp")
    async def get_smtp_settings(
        _admin: UserAccount = Depends(admin_user),
    ) -> dict[str, Any]:
        record = await app.state.store.aget_smtp_settings()
        return smtp_settings_response(record)

    @app.put("/api/v1/settings/smtp")
    async def update_smtp_settings(
        payload: SMTPSettingsRequest,
        request: Request,
        admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        previous = await app.state.store.aget_smtp_settings()
        settings_id = previous.id if previous else uuid.uuid4()
        credentials = smtp_request_credentials(payload)
        if credentials is not None:
            encrypted_credentials = encrypt_smtp_values(settings_id, credentials)
        elif previous is not None:
            encrypted_credentials = previous.encrypted_credentials
        else:
            environment = environment_smtp_config(config)
            if environment.username and environment.password:
                encrypted_credentials = encrypt_smtp_values(
                    settings_id, (environment.username, environment.password)
                )
            else:
                encrypted_credentials = b""

        record = SMTPSettingsRecord(
            id=settings_id,
            host=payload.host,
            port=payload.port,
            use_tls=payload.use_tls,
            from_address=payload.from_address,
            encrypted_credentials=encrypted_credentials,
        )
        if previous is None:
            canonical = await app.state.store.acreate_smtp_settings_if_absent(record)
            if canonical.id != record.id:
                if credentials is None:
                    encrypted_credentials = canonical.encrypted_credentials
                else:
                    encrypted_credentials = encrypt_smtp_values(canonical.id, credentials)
                record = SMTPSettingsRecord(
                    id=canonical.id,
                    host=payload.host,
                    port=payload.port,
                    use_tls=payload.use_tls,
                    from_address=payload.from_address,
                    encrypted_credentials=encrypted_credentials,
                )
                record = await app.state.store.asave_smtp_settings(record)
            else:
                record = canonical
        else:
            record = await app.state.store.asave_smtp_settings(record)
        changed_fields = (
            ["host", "port", "use_tls", "from_address"]
            if previous is None
            else [
                name
                for name in ("host", "port", "use_tls", "from_address")
                if getattr(previous, name) != getattr(record, name)
            ]
        )
        await audit_request(
            request,
            actor_user_id=admin.id,
            action="SMTP_SETTINGS_UPDATED",
            resource_type="settings",
            resource_id=str(record.id),
            result="SUCCESS",
            metadata={"changed_fields": changed_fields},
        )
        return smtp_settings_response(record)

    @app.post("/api/v1/settings/smtp/test")
    async def test_smtp_settings(
        payload: SMTPSettingsRequest,
        _admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        credentials = smtp_request_credentials(payload)
        if credentials is None:
            stored = await app.state.store.aget_smtp_settings()
            credentials = smtp_credentials_for_record(stored)
        server_config = SMTPServerConfig(
            host=payload.host,
            port=payload.port,
            username=credentials[0],
            password=credentials[1],
            from_address=payload.from_address,
            use_tls=payload.use_tls,
        )
        try:
            await asyncio.to_thread(test_smtp_connection, server_config)
        except SMTPConnectionError:
            return {"success": False, "message": "No se pudo conectar con SMTP"}
        return {"success": True, "message": "Conexión SMTP correcta"}

    @app.get("/api/v1/audit-events")
    async def list_audit_events(
        limit: int = Query(default=100, ge=1, le=500),
        _admin: UserAccount = Depends(admin_user),
    ) -> dict[str, Any]:
        events = await app.state.store.alist_audit_events(limit)
        return {"items": events}

    @app.get("/api/v1/notifications/outbox")
    async def list_notification_outbox(
        limit: int = Query(default=100, ge=1, le=200),
        _admin: UserAccount = Depends(admin_user),
    ) -> dict[str, Any]:
        items = await app.state.store.alist_notification_outbox(limit)
        return {"items": items}

    @app.get("/api/v1/connections")
    async def list_connections(user: UserAccount = Depends(current_user)) -> dict[str, Any]:
        connections = await app.state.store.alist_connections(
            None if user.role == "ADMIN" else user.id
        )
        return {"items": [connection.public_dict() for connection in connections]}

    @app.get("/api/v1/system/capabilities")
    async def system_capabilities(_user: UserAccount = Depends(current_user)) -> dict[str, Any]:
        return {
            "enabled_providers": [
                "MOCK",
                "MEGA",
                "GOOGLE_DRIVE",
                "SEAFILE",
                "SMB",
                "NEXTCLOUD",
            ]
        }

    @app.delete("/api/v1/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def archive_connection(
        connection_id: uuid.UUID,
        request: Request,
        admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> Response:
        del user_csrf
        try:
            connection = await app.state.store.aarchive_connection(
                connection_id,
                actor_user_id=admin.id,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
        except ConnectionInUseError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No se puede archivar mientras tenga transferencias activas.",
            ) from exc
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/connections", status_code=status.HTTP_201_CREATED)
    async def create_connection(
        payload: ConnectionCreate,
        request: Request,
        user: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        encrypted_credentials = b""
        connection_id = uuid.uuid4()
        if payload.provider_type != "SEAFILE" and payload.adapter_type != "RCLONE":
            raise HTTPException(
                status_code=422, detail="El adaptador API solo está disponible para Seafile"
            )
        if payload.provider_type != "MOCK":
            if not payload.credentials:
                raise HTTPException(status_code=422, detail="Se requieren credenciales")
            try:
                validation_credentials = dict(payload.credentials)
                if payload.provider_type == "MEGA":
                    MegaCmdProvider.validate_credentials(validation_credentials)
                elif payload.provider_type == "SEAFILE":
                    validation_credentials = validate_seafile_credentials(validation_credentials)
                elif payload.provider_type == "SMB":
                    validation_credentials = RcloneSmbProvider.validate_credentials(
                        validation_credentials
                    )
                    provider_for(
                        ConnectionRecord(
                            id=connection_id,
                            name=payload.name,
                            provider_type="SMB",
                            created_by=user.id,
                            encrypted_credentials=b"",
                        ),
                        validation_credentials,
                    ).config_content()
                elif payload.provider_type == "NEXTCLOUD":
                    validation_credentials = NextcloudRcloneProvider.validate_credentials(
                        validation_credentials
                    )
                    NextcloudRcloneProvider(
                        connection_id,
                        validation_credentials,
                        RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                        chunk_size=config.nextcloud_chunk_size,
                    ).config_content()
                elif payload.provider_type == "GOOGLE_DRIVE":
                    validation_credentials.setdefault("refresh_token", "oauth-pending")
                    RcloneGoogleDriveProvider(
                        connection_id,
                        validation_credentials,
                        RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                    ).config_content()
                payload.credentials = validation_credentials
                encrypted_credentials = CredentialCipher.from_secret_file(
                    config.master_encryption_key_file
                ).encrypt(connection_id, validation_credentials)
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Credenciales inválidas o clave no disponible"
                ) from exc
        connection = await app.state.store.acreate_connection(
            payload.name,
            payload.provider_type,
            user.id,
            connection_id=connection_id,
            encrypted_credentials=encrypted_credentials,
            is_global=payload.is_global,
            adapter_type=payload.adapter_type,
        )
        if connection.provider_type != "MOCK":
            connection.status = "INACTIVE"
            await app.state.store.asave_connection(connection)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="CONNECTION_CREATED",
            resource_type="connection",
            resource_id=str(connection.id),
            result="SUCCESS",
            metadata={
                "provider_type": connection.provider_type,
                "adapter_type": connection.adapter_type,
                "is_global": connection.is_global,
            },
        )
        return cast(dict[str, Any], connection.public_dict())

    @app.post("/api/v1/connections/{connection_id}/google-oauth/start")
    async def start_google_oauth(
        connection_id: uuid.UUID,
        request: Request,
        user: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None or connection.provider_type != "GOOGLE_DRIVE":
            raise HTTPException(status_code=404, detail="Conexión Google Drive no encontrada")
        credentials = credentials_for(connection)
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        redirect_uri = _google_callback_url(config.app_base_url, config.app_env)
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise HTTPException(status_code=409, detail="Faltan las credenciales del cliente OAuth")
        if redirect_uri is None:
            raise HTTPException(
                status_code=503, detail="APP_BASE_URL debe ser una URL HTTPS válida"
            )

        oauth_state = secrets.token_urlsafe(32)
        request.session["google_oauth"] = {
            "state": oauth_state,
            "connection_id": str(connection.id),
            "admin_id": str(user.id),
        }
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": _GOOGLE_DRIVE_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": oauth_state,
            }
        )
        return {"authorization_url": f"{_GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"}

    @app.get("/api/v1/connections/google-oauth/callback")
    async def google_oauth_callback(
        request: Request,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        user: UserAccount = Depends(admin_user),
    ) -> RedirectResponse:
        oauth_session = request.session.pop("google_oauth", None)
        base_url = config.app_base_url.rstrip("/")
        failed_redirect = RedirectResponse(f"{base_url}/?google_oauth=failed", status_code=303)
        if (
            error
            or not isinstance(oauth_session, dict)
            or not isinstance(oauth_session.get("state"), str)
            or not isinstance(state, str)
            or not hmac.compare_digest(oauth_session["state"], state)
            or oauth_session.get("admin_id") != str(user.id)
            or not isinstance(code, str)
            or not code
        ):
            return failed_redirect
        try:
            connection_id = uuid.UUID(str(oauth_session.get("connection_id")))
        except ValueError:
            return failed_redirect
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if connection.provider_type != "GOOGLE_DRIVE":
            return failed_redirect
        credentials = credentials_for(connection)
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        redirect_uri = _google_callback_url(config.app_base_url, config.app_env)
        if (
            not isinstance(client_id, str)
            or not isinstance(client_secret, str)
            or redirect_uri is None
        ):
            return failed_redirect

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                token_response = await client.post(
                    _GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                    },
                )
            token_response.raise_for_status()
            token_payload = token_response.json()
        except (httpx.HTTPError, ValueError):
            return failed_redirect
        if not isinstance(token_payload, dict):
            return failed_redirect
        refresh_token = token_payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return failed_redirect
        credentials["refresh_token"] = refresh_token
        access_token = token_payload.get("access_token")
        if isinstance(access_token, str):
            credentials["access_token"] = access_token
        expires_in = token_payload.get("expires_in")
        if isinstance(expires_in, int) and expires_in > 0:
            credentials["expiry"] = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
        try:
            connection.encrypted_credentials = CredentialCipher.from_secret_file(
                config.master_encryption_key_file
            ).encrypt(connection.id, credentials)
        except (OSError, ValueError):
            return failed_redirect
        connection.status = "INACTIVE"
        connection.last_tested_at = None
        connection.last_test_status = None
        try:
            await app.state.store.asave_connection(connection)
        except ConnectionUnavailableError as exc:
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await audit_request(
            request,
            actor_user_id=user.id,
            action="GOOGLE_OAUTH_CONNECTED",
            resource_type="connection",
            resource_id=str(connection.id),
            result="SUCCESS",
        )
        return RedirectResponse(f"{base_url}/?google_oauth=success", status_code=303)

    @app.patch("/api/v1/connections/{connection_id}")
    async def update_connection(
        connection_id: uuid.UUID,
        payload: ConnectionUpdate,
        request: Request,
        user: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if not payload.model_fields_set:
            raise HTTPException(status_code=422, detail="No hay cambios para aplicar")
        if payload.adapter_type is not None:
            if connection.provider_type != "SEAFILE" and payload.adapter_type != "RCLONE":
                raise HTTPException(
                    status_code=422, detail="El adaptador API solo está disponible para Seafile"
                )
            if payload.adapter_type != connection.adapter_type:
                connection.adapter_type = payload.adapter_type
                connection.status = "INACTIVE"
                connection.last_tested_at = None
                connection.last_test_status = None
        if payload.name is not None:
            connection.name = payload.name.strip()
        if payload.is_global is not None:
            connection.is_global = payload.is_global
        if payload.credentials is not None:
            if connection.provider_type == "MOCK":
                raise HTTPException(
                    status_code=422, detail="Las conexiones simuladas no usan credenciales"
                )
            try:
                if connection.provider_type == "MEGA":
                    MegaCmdProvider.validate_credentials(payload.credentials)
                elif connection.provider_type == "SEAFILE":
                    payload.credentials = validate_seafile_credentials(payload.credentials)
                elif connection.provider_type == "SMB":
                    payload.credentials = RcloneSmbProvider.validate_credentials(
                        payload.credentials
                    )
                    provider_for(connection, payload.credentials).config_content()
                elif connection.provider_type == "NEXTCLOUD":
                    payload.credentials = NextcloudRcloneProvider.validate_credentials(
                        payload.credentials
                    )
                    NextcloudRcloneProvider(
                        connection.id,
                        payload.credentials,
                        RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                        chunk_size=config.nextcloud_chunk_size,
                    ).config_content()
                elif connection.provider_type == "GOOGLE_DRIVE":
                    RcloneGoogleDriveProvider(
                        connection.id,
                        payload.credentials,
                        RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                    ).config_content()
                connection.encrypted_credentials = CredentialCipher.from_secret_file(
                    config.master_encryption_key_file
                ).encrypt(connection.id, payload.credentials)
                connection.status = "INACTIVE"
                connection.last_tested_at = None
                connection.last_test_status = None
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Credenciales inválidas o clave no disponible"
                ) from exc
        try:
            await app.state.store.asave_connection(connection)
        except ConnectionUnavailableError as exc:
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await audit_request(
            request,
            actor_user_id=user.id,
            action="CONNECTION_UPDATED",
            resource_type="connection",
            resource_id=str(connection.id),
            result="SUCCESS",
            metadata={
                "credentials_updated": payload.credentials is not None,
                "adapter_type": connection.adapter_type,
                "is_global": connection.is_global,
            },
        )
        return cast(dict[str, Any], connection.public_dict())

    @app.post("/api/v1/connections/{connection_id}/deactivate")
    async def deactivate_connection(
        connection_id: uuid.UUID,
        request: Request,
        user: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        connection.status = "INACTIVE"
        try:
            await app.state.store.asave_connection(connection)
        except ConnectionUnavailableError as exc:
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await audit_request(
            request,
            actor_user_id=user.id,
            action="CONNECTION_DEACTIVATED",
            resource_type="connection",
            resource_id=str(connection.id),
            result="SUCCESS",
        )
        return {"message": "Conexión desactivada"}

    @app.post("/api/v1/connections/{connection_id}/users/{user_id}")
    async def grant_connection(
        connection_id: uuid.UUID,
        user_id: uuid.UUID,
        request: Request,
        admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, str]:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        target = await app.state.store.aget_user(user_id)
        if connection is None or target is None:
            raise HTTPException(status_code=404, detail="Conexión o usuario no encontrado")
        if not target.is_active:
            raise HTTPException(status_code=409, detail="No se puede asignar a un usuario inactivo")
        try:
            await app.state.store.agrant_connection(user_id, connection_id, admin.id)
        except ConnectionUnavailableError as exc:
            if exc.reason == "inactive":
                raise HTTPException(status_code=409, detail="La conexión está inactiva") from exc
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await audit_request(
            request,
            actor_user_id=admin.id,
            action="CONNECTION_ASSIGNED",
            resource_type="connection",
            resource_id=str(connection_id),
            result="SUCCESS",
            metadata={"user_id": str(user_id)},
        )
        return {"message": "Acceso asignado"}

    @app.delete("/api/v1/connections/{connection_id}/users/{user_id}", status_code=204)
    async def revoke_connection(
        connection_id: uuid.UUID,
        user_id: uuid.UUID,
        request: Request,
        _admin: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> Response:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        target = await app.state.store.aget_user(user_id)
        if connection is None or target is None:
            raise HTTPException(status_code=404, detail="Conexión o usuario no encontrado")
        await app.state.store.arevoke_connection(user_id, connection_id)
        await audit_request(
            request,
            actor_user_id=_admin.id,
            action="CONNECTION_REVOKED",
            resource_type="connection",
            resource_id=str(connection_id),
            result="SUCCESS",
            metadata={"user_id": str(user_id)},
        )
        return Response(status_code=204)

    @app.post("/api/v1/connections/{connection_id}/test")
    async def test_connection(
        connection_id: uuid.UUID,
        request: Request,
        payload: ConnectionTestRequest | None = None,
        user: UserAccount = Depends(admin_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if connection.provider_type == "SEAFILE":
            try:
                credentials = credentials_for(connection)
                auth_client = SeafileAuthClient(credentials["url"])
                otp = payload.otp.strip() if payload and payload.otp else None
                token = credentials.get("auth_token")
                if otp:
                    token = await auth_client.obtain_token(
                        credentials["username"], credentials["password"], otp
                    )
                elif not token or not await auth_client.ping(token):
                    token = await auth_client.obtain_token(
                        credentials["username"], credentials["password"]
                    )
                credentials["auth_token"] = token
                connection.encrypted_credentials = CredentialCipher.from_secret_file(
                    config.master_encryption_key_file
                ).encrypt(connection.id, credentials)
                result = await provider_for(connection, credentials).test_connection()
            except SeafileAuthenticationError:
                result = ConnectionTestResult(
                    success=False,
                    message=(
                        "No se pudo autenticar con Seafile. Verifica las credenciales y el "
                        "código 2FA."
                    ),
                    error_code="SEAFILE_AUTHENTICATION_FAILED",
                )
            except (KeyError, OSError, ValueError):
                result = ConnectionTestResult(
                    success=False,
                    message="No fue posible verificar la conexión Seafile",
                    error_code="PROVIDER_CONNECTION_FAILED",
                )
        else:
            result = await provider_for(connection).test_connection()
        connection.last_tested_at = datetime.now(UTC)
        connection.last_test_status = "SUCCESS" if result.success else "FAILURE"
        connection.status = "ACTIVE" if result.success else "ERROR"
        try:
            await app.state.store.asave_connection(connection)
        except ConnectionUnavailableError as exc:
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await audit_request(
            request,
            actor_user_id=user.id,
            action="CONNECTION_TESTED",
            resource_type="connection",
            resource_id=str(connection_id),
            result="SUCCESS" if result.success else "FAILURE",
        )
        return cast(dict[str, Any], result.model_dump())

    @app.get("/api/v1/connections/{connection_id}/items")
    async def list_items(
        connection_id: uuid.UUID,
        path: str = "/",
        cursor: str | None = None,
        page_size: int = Query(default=50, ge=1, le=200),
        sort: str = "name",
        direction: str = "asc",
        search: str | None = None,
        parent_object_id: str | None = Query(default=None, max_length=512),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        connection = await app.state.store.aget_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if connection.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="La conexión está inactiva")
        if user.role != "ADMIN" and not await app.state.store.ahas_connection_access(
            user.id, connection_id
        ):
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if parent_object_id is not None and any(ord(char) < 32 for char in parent_object_id):
            raise HTTPException(status_code=400, detail="Identificador de carpeta no válido")
        try:
            page = await provider_for(connection).list_items(
                path,
                cursor,
                page_size,
                sort,
                direction,
                search,
                parent_object_id=parent_object_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cast(dict[str, Any], page.model_dump(mode="json"))

    @app.post("/api/v1/transfers", status_code=status.HTTP_201_CREATED)
    async def create_transfer(
        payload: TransferCreate,
        request: Request,
        idempotency_key: uuid.UUID = Header(alias="Idempotency-Key"),
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        if payload.operation != "COPY":
            raise HTTPException(status_code=422, detail="Solo se permite la operación COPY")
        if payload.source_file_error_policy != SourceFileErrorPolicy.STOP:
            raise HTTPException(
                status_code=422,
                detail="La omisión automática de archivos no está disponible todavía.",
            )
        if payload.conflict_policy != "KEEP_BOTH":
            raise HTTPException(
                status_code=422,
                detail=(
                    "Las transferencias nuevas conservan los archivos y numeran los nombres "
                    "repetidos."
                ),
            )
        if payload.source_connection_id == payload.destination_connection_id:
            raise HTTPException(status_code=422, detail="Origen y destino deben ser distintos")
        try:
            safe_paths = [MockStorageProvider.normalize_path(path) for path in payload.source_paths]
            safe_destination = MockStorageProvider.normalize_path(payload.destination_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ruta no válida") from exc
        canonical_intent = payload.model_dump(mode="json")
        canonical_intent["source_paths"] = safe_paths
        canonical_intent["destination_path"] = safe_destination
        request_hash = hashlib.sha256(
            json.dumps(canonical_intent, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            replay = await app.state.store.aget_transfer_by_idempotency(
                user.id, idempotency_key, request_hash
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="La clave de idempotencia ya se usó con otros datos.",
            ) from exc
        if replay is not None:
            if config.app_env.lower() == "production":
                try:
                    from app.worker import dispatch_pending_transfers

                    dispatch_pending_transfers.delay()
                except Exception:
                    pass
            return cast(dict[str, Any], replay.public_dict())

        source = await app.state.store.aget_connection(payload.source_connection_id)
        destination = await app.state.store.aget_connection(payload.destination_connection_id)
        if source is None or destination is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Una conexión está inactiva")
        if destination.provider_type == "SMB":
            raise HTTPException(
                status_code=422,
                detail="SMB solo se admite como origen de una migración.",
            )
        if user.role != "ADMIN":
            for connection_id in (source.id, destination.id):
                if not await app.state.store.ahas_connection_access(user.id, connection_id):
                    raise HTTPException(status_code=404, detail="Conexión no encontrada")
        try:
            if payload.depends_on_transfer_id is not None:
                parent = await app.state.store.aget_transfer(payload.depends_on_transfer_id)
                if parent is None or (parent.requested_by != user.id and user.role != "ADMIN"):
                    raise HTTPException(status_code=404, detail="Transferencia padre no encontrada")
            record = await app.state.store.acreate_transfer(
                user.id,
                source.id,
                destination.id,
                safe_paths,
                safe_destination,
                payload.conflict_policy,
                payload.send_email_notification,
                payload.scheduled_at,
                payload.schedule_timezone,
                payload.execution_mode,
                payload.depends_on_transfer_id,
                idempotency_key,
                request_hash,
                audit_event={
                    "ip_address": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                    "metadata": {
                        "operation": "COPY",
                        "execution_mode": payload.execution_mode,
                        "scheduled_at": (
                            payload.scheduled_at.isoformat() if payload.scheduled_at else None
                        ),
                        "depends_on_transfer_id": (
                            str(payload.depends_on_transfer_id)
                            if payload.depends_on_transfer_id
                            else None
                        ),
                    },
                },
                source_item_ids=payload.source_item_ids,
                source_parent_ids=payload.source_parent_ids,
                source_file_error_policy=payload.source_file_error_policy.value,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="La clave de idempotencia ya se usó con otros datos.",
            ) from exc
        except ConnectionUnavailableError as exc:
            if exc.reason == "inactive":
                raise HTTPException(status_code=409, detail="Una conexión está inactiva") from exc
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        if record.idempotent_replay:
            if config.app_env.lower() == "production":
                try:
                    from app.worker import dispatch_pending_transfers

                    dispatch_pending_transfers.delay()
                except Exception:
                    pass
            return cast(dict[str, Any], record.public_dict())
        await publish_transfer(record)
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_transfers

                dispatch_pending_transfers.delay()
            except Exception:
                # The transfer is persisted first; Celery Beat retries dispatch
                # after a broker outage without losing the scheduled request.
                pass
        elif payload.scheduled_at is None:

            async def should_cancel_transfer() -> bool:
                return await cancel_requested(record.id)

            executor = TransferExecutor(
                config,
                app.state.store,
                publish_transfer,
                should_cancel_transfer,
            )
            task = asyncio.create_task(executor.execute(record.id))
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)
        return cast(dict[str, Any], record.public_dict())

    async def visible_transfer(transfer_id: uuid.UUID, user: UserAccount) -> TransferRecord:
        record = await app.state.store.aget_transfer(transfer_id)
        if record is None or (record.requested_by != user.id and user.role != "ADMIN"):
            raise HTTPException(status_code=404, detail="Transferencia no encontrada")
        return cast(TransferRecord, record)

    async def transfer_item_skip_state(
        record: TransferRecord,
        item: Any,
        user: UserAccount,
    ) -> tuple[bool, str | None]:
        if record.status != "FAILED":
            return False, "TRANSFER_NOT_FAILED"
        if item.is_dir:
            return False, "DIRECTORIES_CANNOT_BE_OMITTED"
        if item.status != "FAILED":
            return False, "ITEM_NOT_FAILED"
        if item.copy_started:
            return False, "DESTINATION_WRITE_MAY_HAVE_STARTED"
        source = await app.state.store.aget_connection(record.source_connection_id)
        destination = await app.state.store.aget_connection(record.destination_connection_id)
        if source is None or destination is None:
            return False, "CONNECTION_UNAVAILABLE"
        record.source_provider_type = source.provider_type
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            return False, "CONNECTION_INACTIVE"
        if (
            source.credential_version != record.source_credential_version
            or destination.credential_version != record.destination_credential_version
        ):
            return False, "CREDENTIALS_CHANGED"
        if (
            record.dispatch_token is not None
            or record.dispatch_claimed_at is not None
            or record.execution_started_at is not None
            or record.execution_heartbeat_at is not None
        ):
            return False, "TRANSFER_EXECUTION_ACTIVE"
        if user.role != "ADMIN":
            for connection_id in (source.id, destination.id):
                if not await app.state.store.ahas_connection_access(user.id, connection_id):
                    return False, "CONNECTION_ACCESS_CHANGED"
        events = await app.state.store.alist_transfer_events(record.id)
        basename = PurePosixPath(item.source_path).name
        basename_matches = await app.state.store.acount_transfer_manifest_basename_matches(
            record.id, basename
        )
        failure_stage, failure_attempt, failure_retryable = resolve_failure_evidence(
            record,
            item,
            events,
            basename_matches=basename_matches,
        )
        unavailable = skip_unavailable_reason(
            record,
            item,
            failure_stage=failure_stage,
            failure_attempt=failure_attempt,
            failure_retryable=failure_retryable,
        )
        return unavailable is None, unavailable

    async def transfer_item_retry_omission_state(
        record: TransferRecord,
        item: Any,
        user: UserAccount,
    ) -> bool:
        if record.status not in {"FAILED", "CANCELLED", "COMPLETED_WITH_WARNINGS"}:
            return False
        if item.is_dir or item.status != "SKIPPED" or item.omission_kind is None:
            return False
        if item.omitted_at is None or item.copy_started:
            return False
        if (
            record.dispatch_token is not None
            or record.dispatch_claimed_at is not None
            or record.execution_started_at is not None
            or record.execution_heartbeat_at is not None
        ):
            return False
        source = await app.state.store.aget_connection(record.source_connection_id)
        destination = await app.state.store.aget_connection(record.destination_connection_id)
        if source is None or destination is None:
            return False
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            return False
        if (
            source.credential_version != record.source_credential_version
            or destination.credential_version != record.destination_credential_version
        ):
            return False
        if user.role != "ADMIN":
            for connection_id in (source.id, destination.id):
                if not await app.state.store.ahas_connection_access(user.id, connection_id):
                    return False
        return True

    @app.get("/api/v1/transfers/capabilities")
    async def transfer_capabilities(
        _user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        return {
            "scheduled_transfers": True,
            "recurrence": False,
            "execution_modes": ["AUTO", "EXCLUSIVE"],
            "max_parallel_transfers": config.transfer_max_concurrent,
            "max_files_by_source_provider": {
                "default": config.transfer_max_files,
                "SMB": config.smb_transfer_max_files,
            },
            "schedule_timezone_format": "IANA",
        }

    @app.post(
        "/api/v1/transfers/{transfer_id}/evaluation",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def request_transfer_evaluation(
        transfer_id: uuid.UUID,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        await visible_transfer(transfer_id, user)
        try:
            evaluation = await app.state.store.arequest_transfer_evaluation(
                transfer_id, datetime.now(UTC)
            )
        except EvaluationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = await visible_transfer(transfer_id, user)
        await publish_transfer(updated)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_EVALUATION_REQUESTED",
            resource_type="transfer",
            resource_id=str(transfer_id),
            result="SUCCESS",
            metadata={
                "generation": evaluation.generation,
                "status": evaluation.status,
            },
        )
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_evaluations

                dispatch_pending_evaluations.delay()
            except Exception:
                pass
        return cast(dict[str, Any], evaluation.public_dict())

    @app.patch("/api/v1/transfers/{transfer_id}")
    async def update_transfer_schedule(
        transfer_id: uuid.UUID,
        payload: TransferScheduleUpdate,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        record = await visible_transfer(transfer_id, user)
        if record.status not in {"SCHEDULED", "QUEUED"} or record.dispatch_token is not None:
            raise HTTPException(
                status_code=409,
                detail="Solo se puede reprogramar una transferencia pendiente de ejecución.",
            )
        if payload.depends_on_transfer_id is not None:
            parent = await app.state.store.aget_transfer(payload.depends_on_transfer_id)
            if parent is None or (parent.requested_by != user.id and user.role != "ADMIN"):
                raise HTTPException(status_code=404, detail="Transferencia padre no encontrada")
        try:
            updated = await app.state.store.aupdate_transfer_schedule(
                transfer_id,
                scheduled_at=payload.scheduled_at,
                schedule_timezone=payload.schedule_timezone,
                execution_mode=payload.execution_mode,
                depends_on_transfer_id=payload.depends_on_transfer_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "La transferencia ya está admitida para ejecución y no se puede reprogramar."
                ),
            )
        await publish_transfer(updated)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_RESCHEDULED",
            resource_type="transfer",
            resource_id=str(transfer_id),
            result="SUCCESS",
            metadata={
                "scheduled_at": updated.scheduled_at.isoformat() if updated.scheduled_at else None,
                "execution_mode": updated.execution_mode,
                "depends_on_transfer_id": (
                    str(updated.depends_on_transfer_id) if updated.depends_on_transfer_id else None
                ),
            },
        )
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_transfers

                dispatch_pending_transfers.delay()
            except Exception:
                pass
        return cast(dict[str, Any], updated.public_dict())

    @app.get("/api/v1/transfer-incidents")
    async def list_transfer_incidents(
        kind: Literal["failed", "omitted"] = Query(default="failed"),
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=256),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        decoded_cursor = decode_transfer_incident_cursor(cursor)
        before_at, before_id = decoded_cursor if decoded_cursor else (None, None)
        requested_by = None if user.role == "ADMIN" else user.id
        counts = await app.state.store.acount_transfer_incidents(requested_by)
        incidents = await app.state.store.alist_transfer_incidents(
            requested_by,
            kind=kind,
            before_at=before_at,
            before_id=before_id,
            limit=limit + 1,
        )
        has_more = len(incidents) > limit
        incidents = incidents[:limit]
        transfer_ids = {incident.transfer.id for incident in incidents}
        failure_events = await app.state.store.alist_transfer_failure_diagnostics(transfer_ids)
        failed_candidates = [
            (incident.transfer.id, PurePosixPath(incident.item.source_path).name)
            for incident in incidents
            if incident.item.status == "FAILED"
        ]
        basename_counts = await app.state.store.acount_transfer_manifest_basename_matches_bulk(
            failed_candidates
        )
        connection_ids = {
            connection.id
            for incident in incidents
            for connection in (incident.source_connection, incident.destination_connection)
            if connection is not None
        }
        accessible_ids = (
            connection_ids
            if user.role == "ADMIN"
            else await app.state.store.alist_accessible_connection_ids(user.id, connection_ids)
        )

        public_items = []
        for incident in incidents:
            transfer = incident.transfer
            item = incident.item
            source = incident.source_connection
            destination = incident.destination_connection
            transfer.source_provider_type = source.provider_type if source else None
            basename = PurePosixPath(item.source_path).name
            failure_stage, failure_attempt, failure_retryable = resolve_failure_evidence(
                transfer,
                item,
                failure_events.get(transfer.id, []),
                basename_matches=basename_counts.get((transfer.id, basename), 0),
            )
            unavailable_reason = skip_unavailable_reason(
                transfer,
                item,
                failure_stage=failure_stage,
                failure_attempt=failure_attempt,
                failure_retryable=failure_retryable,
            )
            if unavailable_reason is None:
                if source is None or destination is None:
                    unavailable_reason = "CONNECTION_UNAVAILABLE"
                elif source.status != "ACTIVE" or destination.status != "ACTIVE":
                    unavailable_reason = "CONNECTION_INACTIVE"
                elif (
                    source.credential_version != transfer.source_credential_version
                    or destination.credential_version != transfer.destination_credential_version
                ):
                    unavailable_reason = "CREDENTIALS_CHANGED"
                elif source.id not in accessible_ids or destination.id not in accessible_ids:
                    unavailable_reason = "CONNECTION_ACCESS_CHANGED"

            can_retry_omission = bool(
                transfer.status in {"FAILED", "CANCELLED", "COMPLETED_WITH_WARNINGS"}
                and not item.is_dir
                and item.status == "SKIPPED"
                and item.omission_kind is not None
                and item.omitted_at is not None
                and not item.copy_started
                and transfer.dispatch_token is None
                and transfer.dispatch_claimed_at is None
                and transfer.execution_started_at is None
                and transfer.execution_heartbeat_at is None
                and source is not None
                and destination is not None
                and source.status == "ACTIVE"
                and destination.status == "ACTIVE"
                and source.credential_version == transfer.source_credential_version
                and destination.credential_version == transfer.destination_credential_version
                and source.id in accessible_ids
                and destination.id in accessible_ids
            )
            public_items.append(
                {
                    "transfer": {
                        "id": str(transfer.id),
                        "public_id": transfer.public_id,
                        "status": transfer.status,
                        "attempt_count": transfer.attempt_count,
                        "source_connection": {
                            "name": source.name,
                            "provider_type": source.provider_type,
                        }
                        if source
                        else None,
                        "destination_connection": {
                            "name": destination.name,
                            "provider_type": destination.provider_type,
                        }
                        if destination
                        else None,
                    },
                    "item": {
                        "id": str(item.id),
                        "source_path": item.source_path,
                        "destination_path": item.destination_path,
                        "requested_destination_path": item.requested_destination_path,
                        "rename_reason": item.rename_reason,
                        "status": item.status,
                        "size_bytes": item.size_bytes,
                        "error_code": item.error_code,
                        "failure_stage": failure_stage,
                        "omission_kind": item.omission_kind,
                        "omitted_at": item.omitted_at.isoformat() if item.omitted_at else None,
                        "incident_at": incident.incident_at.isoformat(),
                    },
                    "can_skip": unavailable_reason is None,
                    "skip_unavailable_reason": unavailable_reason,
                    "can_retry_omission": can_retry_omission,
                }
            )

        next_cursor = (
            encode_transfer_incident_cursor(incidents[-1].incident_at, incidents[-1].item.id)
            if has_more and incidents
            else None
        )
        return {"items": public_items, "counts": counts, "next_cursor": next_cursor}

    @app.get("/api/v1/transfers")
    async def list_transfers(
        offset: int = Query(default=0, ge=0),
        page_size: int = Query(default=50, ge=1, le=200),
        pending_only: bool = Query(default=False),
        background_only: bool = Query(default=False),
        exclude_background: bool = Query(default=False),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        user_filter = None if user.role == "ADMIN" else user.id
        page_records, total_count = await app.state.store.alist_transfer_page(
            user_filter,
            offset=offset,
            limit=page_size,
            pending_only=pending_only,
            background_only=background_only,
            exclude_background=exclude_background,
        )
        await attach_pause_capabilities(page_records)
        summaries = await app.state.store.alist_transfer_manifest_summaries(
            [record.id for record in page_records]
        )
        items = []
        for record in page_records:
            item = record.public_dict()
            item["manifest_summary"] = summaries.get(
                record.id,
                {
                    "copied": 0,
                    "failed": 0,
                    "skipped": 0,
                    "pending": 0,
                    "renamed": 0,
                    "omitted": 0,
                    "omitted_bytes": 0,
                },
            )
            items.append(item)
        return {
            "items": items,
            "total_count": total_count,
        }

    @app.get("/api/v1/transfers/{transfer_id}")
    async def get_transfer(
        transfer_id: uuid.UUID, user: UserAccount = Depends(current_user)
    ) -> dict[str, Any]:
        record = await visible_transfer(transfer_id, user)
        await attach_pause_capabilities([record])
        record.manifest_summary = (
            await app.state.store.alist_transfer_manifest_summaries([record.id])
        )[record.id]
        return record.public_dict()

    @app.get("/api/v1/transfers/{transfer_id}/events")
    async def list_transfer_events(
        transfer_id: uuid.UUID, user: UserAccount = Depends(current_user)
    ) -> dict[str, Any]:
        record = await visible_transfer(transfer_id, user)
        return {"items": await app.state.store.alist_transfer_events(record.id)}

    @app.get("/api/v1/transfers/{transfer_id}/items")
    async def list_transfer_items(
        transfer_id: uuid.UUID,
        after_id: uuid.UUID | None = None,
        limit: int = Query(default=200, ge=1, le=200),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        record = await visible_transfer(transfer_id, user)
        items = await app.state.store.alist_transfer_manifest_items(
            record.id,
            statuses={"DIRECTORY", "PENDING", "COPIED", "SKIPPED", "FAILED"},
            after_id=after_id,
            limit=limit,
            include_directories=True,
        )
        public_items = []
        for item in items:
            public_item = item.public_dict()
            can_skip, unavailable_reason = await transfer_item_skip_state(record, item, user)
            public_item["can_skip"] = can_skip
            public_item["skip_unavailable_reason"] = unavailable_reason
            public_item["can_retry_omission"] = await transfer_item_retry_omission_state(
                record, item, user
            )
            public_items.append(public_item)
        return {
            "items": public_items,
            "next_cursor": str(items[-1].id) if len(items) == limit else None,
            "counts": await app.state.store.aget_transfer_manifest_counts(record.id),
        }

    @app.get("/api/v1/transfers/{transfer_id}/omissions.csv")
    async def download_transfer_omissions_csv(
        transfer_id: uuid.UUID,
        user: UserAccount = Depends(current_user),
    ) -> StreamingResponse:
        record = await visible_transfer(transfer_id, user)
        if record.status not in {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}:
            raise HTTPException(
                status_code=409,
                detail="El informe de omisiones está disponible cuando termina la transferencia.",
            )
        return StreamingResponse(
            iter_omissions_csv(app.state.store, record.id),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{record.public_id}-omissions.csv"'
            },
        )

    @app.post("/api/v1/transfers/{transfer_id}/pause", status_code=status.HTTP_202_ACCEPTED)
    async def pause_transfer(
        transfer_id: uuid.UUID,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        record = await visible_transfer(transfer_id, user)
        if record.status not in {"PAUSED", "PAUSING"}:
            source = await app.state.store.aget_connection(record.source_connection_id)
            destination = await app.state.store.aget_connection(record.destination_connection_id)
            if source is None or destination is None:
                raise HTTPException(status_code=409, detail="No se encontraron las conexiones")
            unstarted = (
                record.status in {"QUEUED", "SCHEDULED"} and record.execution_started_at is None
            )
            supports_boundary_pause = (
                record.conflict_policy == "KEEP_BOTH"
                or source.provider_type == "SMB"
                or (source.provider_type == destination.provider_type == "MOCK")
            )
            if not unstarted and not supports_boundary_pause:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "LEGACY_BULK_EXECUTION",
                        "message": (
                            "Esta ejecución no tiene límites verificables entre archivos; "
                            "puede cancelarla o dejar que termine."
                        ),
                    },
                )
        paused, needs_worker = await app.state.store.apause_transfer(transfer_id, datetime.now(UTC))
        if paused is None:
            latest = await app.state.store.aget_transfer(transfer_id)
            if latest is not None:
                await attach_pause_capabilities([latest])
                if latest.pause_unavailable_reason is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": latest.pause_unavailable_reason,
                            "message": (
                                "La transferencia pasó a una ejecución sin límites seguros "
                                "entre archivos."
                            ),
                        },
                    )
            raise HTTPException(status_code=409, detail="La transferencia ya no se puede pausar")
        await publish_transfer(paused)
        if paused.status not in {"PAUSING", "PAUSED"}:
            raise HTTPException(status_code=409, detail="La transferencia ya no se puede pausar")
        if paused.status == "PAUSING" and needs_worker and app.state.event_bus:
            try:
                await app.state.event_bus.publish_progress(
                    str(paused.id),
                    {
                        **paused.public_dict(),
                        "job_id": str(paused.id),
                        "current_speed_bps": paused.current_speed_bps,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception:
                # PostgreSQL stores the request; the boundary checkpoint reads it directly.
                pass
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_PAUSE_REQUESTED",
            resource_type="transfer",
            resource_id=str(paused.id),
            result="SUCCESS",
            metadata={"public_id": paused.public_id, "status": paused.status},
        )
        return cast(dict[str, Any], paused.public_dict())

    @app.post("/api/v1/transfers/{transfer_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    async def resume_transfer(
        transfer_id: uuid.UUID,
        request: Request,
        payload: ResumeTransferRequest | None = None,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del payload
        del user_csrf
        record = await visible_transfer(transfer_id, user)
        if record.status not in {"FAILED", "CANCELLED", "PAUSED"}:
            raise HTTPException(status_code=409, detail="La transferencia no se puede reanudar")
        if record.source_credential_version < 1 or record.destination_credential_version < 1:
            raise HTTPException(
                status_code=409, detail="La transferencia no tiene una versión de origen válida"
            )
        source = await app.state.store.aget_connection(record.source_connection_id)
        destination = await app.state.store.aget_connection(record.destination_connection_id)
        if source is None or destination is None:
            raise HTTPException(
                status_code=409, detail="No se puede reanudar: una conexión ya no existe"
            )
        if source.provider_type != "SMB" and record.conflict_policy != "KEEP_BOTH":
            raise HTTPException(
                status_code=409,
                detail="Esta transferencia no conserva un manifiesto compatible con reanudación.",
            )
        if (
            source.credential_version != record.source_credential_version
            or destination.credential_version != record.destination_credential_version
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Las credenciales o la raíz SMB cambiaron; cree una nueva transferencia."
                    if source.provider_type == "SMB"
                    else "Las credenciales cambiaron; cree una nueva transferencia."
                ),
            )
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Una conexión está inactiva")
        if user.role != "ADMIN":
            for connection_id in (source.id, destination.id):
                if not await app.state.store.ahas_connection_access(user.id, connection_id):
                    raise HTTPException(status_code=404, detail="Transferencia no encontrada")
        counts = await app.state.store.aget_transfer_manifest_counts(record.id)
        if (
            counts["pending"] == 0
            and counts["failed"] == 0
            and not (record.status == "PAUSED" and counts.get("total", 0) == 0)
        ):
            raise HTTPException(status_code=409, detail="No hay elementos pendientes para reanudar")

        resumed = await app.state.store.aresume_transfer(transfer_id)
        if resumed is None:
            raise HTTPException(status_code=409, detail="La transferencia ya no se puede reanudar")
        record = cast(TransferRecord, resumed)
        if app.state.event_bus:
            try:
                await app.state.event_bus.clear_cancel(str(transfer_id))
            except Exception:
                # The database flag was cleared by resume; Redis is only a hint.
                pass
        app.state.cancel_requests.discard(transfer_id)
        await publish_transfer(record)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_RESUMED",
            resource_type="transfer",
            resource_id=str(record.id),
            result="SUCCESS",
            metadata={"public_id": record.public_id, "attempt_count": record.attempt_count},
        )
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_transfers

                dispatch_pending_transfers.delay()
            except Exception:
                # The resumed job remains queued in PostgreSQL for the next Beat tick.
                pass
        else:

            async def should_cancel_resumed_transfer() -> bool:
                return await cancel_requested(record.id)

            executor = TransferExecutor(
                config,
                app.state.store,
                publish_transfer,
                should_cancel_resumed_transfer,
            )
            task = asyncio.create_task(executor.execute(record.id))
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)
        return record.public_dict()

    @app.post(
        "/api/v1/transfers/{transfer_id}/items/{item_id}/skip-and-resume",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def skip_transfer_item_and_resume(
        transfer_id: uuid.UUID,
        item_id: uuid.UUID,
        payload: SkipAndResumeRequest,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        await visible_transfer(transfer_id, user)
        try:
            resumed = await skip_and_resume_transfer(
                app.state.store,
                transfer_id,
                item_id,
                actor_id=user.id,
                request=payload,
            )
        except TransferRecoveryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Transferencia no encontrada") from exc
        except (TransferRecoveryConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if resumed.idempotent_replay:
            resumed.manifest_summary = (
                await app.state.store.alist_transfer_manifest_summaries([resumed.id])
            )[resumed.id]
            return resumed.public_dict()

        if app.state.event_bus:
            try:
                await app.state.event_bus.clear_cancel(str(transfer_id))
            except Exception:
                pass
        app.state.cancel_requests.discard(transfer_id)
        await publish_transfer(resumed)
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_transfers

                dispatch_pending_transfers.delay()
            except Exception:
                # PostgreSQL keeps the queued attempt for the next Beat tick.
                pass
        else:

            async def should_cancel_recovered_transfer() -> bool:
                return await cancel_requested(resumed.id)

            executor = TransferExecutor(
                config,
                app.state.store,
                publish_transfer,
                should_cancel_recovered_transfer,
            )
            task = asyncio.create_task(executor.execute(resumed.id))
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)
        return resumed.public_dict()

    @app.post(
        "/api/v1/transfers/{transfer_id}/retry-omitted",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_omitted_transfer_files(
        transfer_id: uuid.UUID,
        payload: RetryOmittedRequest,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        await visible_transfer(transfer_id, user)
        try:
            retried = await retry_omitted_transfer_items(
                app.state.store,
                transfer_id,
                actor_id=user.id,
                request=payload,
            )
        except TransferRecoveryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Transferencia no encontrada") from exc
        except (TransferRecoveryConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        retried.manifest_summary = (
            await app.state.store.alist_transfer_manifest_summaries([retried.id])
        )[retried.id]
        if retried.idempotent_replay:
            return retried.public_dict()
        if app.state.event_bus:
            try:
                await app.state.event_bus.clear_cancel(str(transfer_id))
            except Exception:
                pass
        app.state.cancel_requests.discard(transfer_id)
        await publish_transfer(retried)
        if config.app_env.lower() == "production":
            try:
                from app.worker import dispatch_pending_transfers

                dispatch_pending_transfers.delay()
            except Exception:
                # PostgreSQL keeps the queued attempt for the next Beat tick.
                pass
        elif retried.depends_on_transfer_id is None:

            async def should_cancel_retried_transfer() -> bool:
                return await cancel_requested(retried.id)

            executor = TransferExecutor(
                config,
                app.state.store,
                publish_transfer,
                should_cancel_retried_transfer,
            )
            task = asyncio.create_task(executor.execute(retried.id))
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)
        return retried.public_dict()

    @app.post(
        "/api/v1/transfers/{transfer_id}/accept-parent-omissions",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def accept_transfer_parent_omissions(
        transfer_id: uuid.UUID,
        payload: AcceptParentOmissionsRequest,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        await visible_transfer(transfer_id, user)
        try:
            accepted = await accept_parent_omissions(
                app.state.store,
                transfer_id,
                actor_id=user.id,
                request=payload,
            )
        except TransferRecoveryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Transferencia no encontrada") from exc
        except (TransferRecoveryConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        accepted.manifest_summary = (
            await app.state.store.alist_transfer_manifest_summaries([accepted.id])
        )[accepted.id]
        if accepted.idempotent_replay:
            return accepted.public_dict()

        await publish_transfer(accepted)
        if config.app_env.lower() == "production":
            try:
                if accepted.evaluation is not None and accepted.evaluation.status == "PENDING":
                    from app.worker import dispatch_pending_evaluations

                    dispatch_pending_evaluations.delay()
                else:
                    from app.worker import dispatch_pending_transfers

                    dispatch_pending_transfers.delay()
            except Exception:
                # The next scheduler tick will pick up the accepted child.
                pass
        return accepted.public_dict()

    @app.get("/api/v1/transfers/{transfer_id}/stream")
    async def stream_transfer(
        transfer_id: uuid.UUID, user: UserAccount = Depends(current_user)
    ) -> StreamingResponse:
        await visible_transfer(transfer_id, user)
        queue = app.state.event_hub.subscribe(str(transfer_id)) if not app.state.event_bus else None
        pubsub = None
        if app.state.redis_client:
            pubsub = app.state.redis_client.pubsub()
            channel = app.state.event_bus.progress_channel(str(transfer_id))
            try:
                await pubsub.subscribe(channel)
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=1)
                    if (
                        message is not None
                        and message.get("type") == "subscribe"
                        and message.get("channel") == channel
                    ):
                        break
                else:
                    raise HTTPException(
                        status_code=503, detail="No se pudo abrir el canal de progreso"
                    )
            except BaseException:
                await pubsub.aclose()
                raise

        async def event_source() -> AsyncIterator[str]:
            try:
                # Subscribe first, then take a fresh snapshot. Any transition before the
                # subscription is reflected in this read; later transitions are buffered.
                snapshot = await visible_transfer(transfer_id, user)
                await attach_pause_capabilities([snapshot])
                snapshot.manifest_summary = (
                    await app.state.store.alist_transfer_manifest_summaries([snapshot.id])
                )[snapshot.id]
                latest = transfer_event(snapshot)
                yield f"data: {json.dumps(latest, separators=(',', ':'))}\n\n"
                if snapshot.status in {
                    "COMPLETED",
                    "COMPLETED_WITH_WARNINGS",
                    "FAILED",
                    "CANCELLED",
                    "PAUSED",
                }:
                    return
                while True:
                    if pubsub:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=15
                        )
                        if message is None:
                            yield ": keep-alive\n\n"
                            continue
                        try:
                            event = json.loads(message["data"])
                        except (KeyError, TypeError, json.JSONDecodeError):
                            continue
                    else:
                        assert queue is not None
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=15)
                        except TimeoutError:
                            yield ": keep-alive\n\n"
                            continue
                    # Pub/Sub may hold a valid transition published during the snapshot
                    # read. Do not compare it to a synthetic wall-clock timestamp here:
                    # duplicates are recoverable, a discarded terminal event is not.
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    if event["status"] in {
                        "COMPLETED",
                        "COMPLETED_WITH_WARNINGS",
                        "FAILED",
                        "CANCELLED",
                        "PAUSED",
                    }:
                        return
            finally:
                if queue is not None:
                    app.state.event_hub.unsubscribe(str(transfer_id), queue)
                if pubsub:
                    await pubsub.unsubscribe(app.state.event_bus.progress_channel(str(transfer_id)))
                    await pubsub.aclose()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/transfers/{transfer_id}/cancel")
    async def cancel_transfer(
        transfer_id: uuid.UUID,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        record = await visible_transfer(transfer_id, user)
        if record.status in {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}:
            raise HTTPException(status_code=409, detail="La transferencia ya terminó")
        record, needs_worker_signal = await app.state.store.acancel_transfer(
            transfer_id, datetime.now(UTC)
        )
        if record is None:
            raise HTTPException(status_code=409, detail="La transferencia ya terminó")
        if needs_worker_signal:
            if app.state.event_bus:
                try:
                    await app.state.event_bus.request_cancel(str(transfer_id))
                except Exception:
                    # PostgreSQL already contains the durable cancellation request.
                    pass
            else:
                app.state.cancel_requests.add(transfer_id)
        await publish_transfer(record)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_CANCELLED",
            resource_type="transfer",
            resource_id=str(transfer_id),
            result="SUCCESS",
        )
        return cast(dict[str, Any], record.public_dict())

    return app


app = create_app()
