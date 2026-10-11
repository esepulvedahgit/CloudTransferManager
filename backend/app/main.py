import asyncio
import hmac
import json
import os
import secrets
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from app.providers.base import ConnectionTestResult
from app.providers.megacmd import MegaCmdProvider
from app.providers.mock import MockStorageProvider
from app.providers.rclone import RcloneGoogleDriveProvider
from app.providers.seafile_auth import (
    SeafileAuthClient,
    SeafileAuthenticationError,
    normalize_seafile_base_url,
)
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
    MemoryStore,
    TransferRecord,
    UserAccount,
)
from app.services.rclone import RcloneRunner
from app.services.transfer_executor import TransferExecutor
from app.settings import Settings, get_settings


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return PasswordService().hash(secrets.token_urlsafe(32))


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
    provider_type: str = Field(pattern="^(MOCK|MEGA|GOOGLE_DRIVE|SEAFILE)$")
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
    source_paths: list[str] = Field(min_length=1, max_length=100)
    destination_path: str = Field(min_length=1, max_length=1024)
    operation: str = "COPY"
    conflict_policy: str = "OVERWRITE_IF_DIFFERENT"
    send_email_notification: bool = False


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

    async def publish_transfer(record: TransferRecord) -> None:
        await app.state.store.asave_transfer(record)
        event = transfer_event(record)
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
            item_path=record.current_item,
        )
        if app.state.event_bus:
            await app.state.event_bus.publish_progress(str(record.id), event)
        else:
            await app.state.event_hub.publish(str(record.id), event)

    async def cancel_requested(transfer_id: uuid.UUID) -> bool:
        if app.state.event_bus:
            return bool(await app.state.event_bus.cancellation_requested(str(transfer_id)))
        return transfer_id in app.state.cancel_requests

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
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Dependencias no disponibles") from exc
        return {"status": "ready"}

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
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = now + timedelta(minutes=15)
            await app.state.store.asave_user(user)
            await audit_request(
                request,
                actor_user_id=user.id,
                action="LOGIN",
                resource_type="user",
                resource_id=str(user.id),
                result="FAILURE",
            )
            raise invalid
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        await app.state.store.asave_user(user)
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
        user.password_hash = app.state.password_service.hash(payload.new_password)
        user.must_change_password = False
        await app.state.store.asave_user(user)
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
        target = await app.state.store.aget_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        if target.id == admin.id:
            raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta")
        if target.role == "ADMIN" and target.is_active:
            if await app.state.store.acount_active_admins() <= 1:
                raise HTTPException(
                    status_code=409, detail="No se puede desactivar al último administrador"
                )
        target.is_active = False
        await app.state.store.asave_user(target)
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
        target = await app.state.store.aget_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        if not payload.model_fields_set:
            raise HTTPException(status_code=422, detail="No hay cambios para aplicar")
        removing_admin = payload.is_active is False or payload.role == "USER"
        if target.is_active and target.role == "ADMIN" and removing_admin:
            if await app.state.store.acount_active_admins() <= 1:
                raise HTTPException(
                    status_code=409, detail="No se puede quitar al último administrador"
                )
        if target.id == admin.id and payload.is_active is False:
            raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta")
        if payload.username is not None:
            target.username = payload.username.strip().casefold()
        if payload.email is not None:
            target.email = payload.email.strip().casefold()
        if payload.role is not None:
            target.role = payload.role
        if payload.is_active is not None:
            target.is_active = payload.is_active
        try:
            await app.state.store.asave_user(target)
        except (IntegrityError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="El usuario o correo ya existe") from exc
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
        target = await app.state.store.aget_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        target.password_hash = app.state.password_service.hash(payload.password)
        target.must_change_password = True
        target.failed_login_attempts = 0
        target.locked_until = None
        await app.state.store.asave_user(target)
        await audit_request(
            request,
            actor_user_id=_admin.id,
            action="PASSWORD_RESET",
            resource_type="user",
            resource_id=str(target.id),
            result="SUCCESS",
        )
        return {"message": "Contraseña restablecida"}

    @app.get("/api/v1/audit-events")
    async def list_audit_events(
        limit: int = Query(default=100, ge=1, le=500),
        _admin: UserAccount = Depends(admin_user),
    ) -> dict[str, Any]:
        events = await app.state.store.alist_audit_events(limit)
        return {"items": events}

    @app.get("/api/v1/connections")
    async def list_connections(user: UserAccount = Depends(current_user)) -> dict[str, Any]:
        connections = await app.state.store.alist_connections(
            None if user.role == "ADMIN" else user.id
        )
        return {"items": [connection.public_dict() for connection in connections]}

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
                else:
                    validation_credentials.setdefault("refresh_token", "oauth-pending")
                    RcloneGoogleDriveProvider(
                        connection_id,
                        validation_credentials,
                        RcloneRunner(config.rclone_binary, config.transfer_temp_path),
                    ).config_content()
                encrypted_credentials = CredentialCipher.from_secret_file(
                    config.master_encryption_key_file
                ).encrypt(connection_id, payload.credentials)
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
                else:
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
        try:
            page = await provider_for(connection).list_items(
                path, cursor, page_size, sort, direction, search
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cast(dict[str, Any], page.model_dump(mode="json"))

    @app.post("/api/v1/transfers", status_code=status.HTTP_201_CREATED)
    async def create_transfer(
        payload: TransferCreate,
        request: Request,
        user: UserAccount = Depends(current_user),
        user_csrf: None = Depends(csrf_guard),
    ) -> dict[str, Any]:
        del user_csrf
        if payload.operation != "COPY":
            raise HTTPException(status_code=422, detail="Solo se permite la operación COPY")
        if payload.conflict_policy not in {"SKIP", "OVERWRITE_IF_DIFFERENT"}:
            raise HTTPException(status_code=422, detail="Política de conflicto no válida")
        if payload.source_connection_id == payload.destination_connection_id:
            raise HTTPException(status_code=422, detail="Origen y destino deben ser distintos")
        source = await app.state.store.aget_connection(payload.source_connection_id)
        destination = await app.state.store.aget_connection(payload.destination_connection_id)
        if source is None or destination is None:
            raise HTTPException(status_code=404, detail="Conexión no encontrada")
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Una conexión está inactiva")
        if user.role != "ADMIN":
            for connection_id in (source.id, destination.id):
                if not await app.state.store.ahas_connection_access(user.id, connection_id):
                    raise HTTPException(status_code=404, detail="Conexión no encontrada")
        try:
            safe_paths = [MockStorageProvider.normalize_path(path) for path in payload.source_paths]
            safe_destination = MockStorageProvider.normalize_path(payload.destination_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ruta no válida") from exc
        for path in safe_paths:
            if await provider_for(source).get_item(path) is None:
                raise HTTPException(
                    status_code=400, detail=f"No existe el elemento de origen: {path}"
                )
        try:
            record = await app.state.store.acreate_transfer(
                user.id,
                source.id,
                destination.id,
                safe_paths,
                safe_destination,
                payload.conflict_policy,
                payload.send_email_notification,
            )
        except ConnectionUnavailableError as exc:
            if exc.reason == "inactive":
                raise HTTPException(status_code=409, detail="Una conexión está inactiva") from exc
            raise HTTPException(status_code=404, detail="Conexión no encontrada") from exc
        await publish_transfer(record)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_CREATED",
            resource_type="transfer",
            resource_id=str(record.id),
            result="SUCCESS",
            metadata={"public_id": record.public_id, "operation": "COPY"},
        )
        if config.app_env.lower() == "production":
            try:
                from app.worker import run_transfer

                run_transfer.delay(str(record.id))
            except Exception as exc:
                record.status = "FAILED"
                record.error_count += 1
                record.completed_at = datetime.now(UTC)
                await publish_transfer(record)
                raise HTTPException(
                    status_code=503, detail="No se pudo agendar la transferencia"
                ) from exc
        else:

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

    @app.get("/api/v1/transfers")
    async def list_transfers(
        offset: int = Query(default=0, ge=0),
        page_size: int = Query(default=50, ge=1, le=200),
        user: UserAccount = Depends(current_user),
    ) -> dict[str, Any]:
        user_filter = None if user.role == "ADMIN" else user.id
        records = await app.state.store.alist_transfers(user_filter)
        return {
            "items": [record.public_dict() for record in records[offset : offset + page_size]],
            "total_count": len(records),
        }

    @app.get("/api/v1/transfers/{transfer_id}")
    async def get_transfer(
        transfer_id: uuid.UUID, user: UserAccount = Depends(current_user)
    ) -> dict[str, Any]:
        record = await visible_transfer(transfer_id, user)
        return record.public_dict()

    @app.get("/api/v1/transfers/{transfer_id}/events")
    async def list_transfer_events(
        transfer_id: uuid.UUID, user: UserAccount = Depends(current_user)
    ) -> dict[str, Any]:
        record = await visible_transfer(transfer_id, user)
        return {"items": await app.state.store.alist_transfer_events(record.id)}

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
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=False, timeout=1
                    )
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
                latest = transfer_event(snapshot)
                yield f"data: {json.dumps(latest, separators=(',', ':'))}\n\n"
                if snapshot.status in {
                    "COMPLETED",
                    "COMPLETED_WITH_WARNINGS",
                    "FAILED",
                    "CANCELLED",
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
        if app.state.event_bus:
            await app.state.event_bus.request_cancel(str(transfer_id))
        else:
            app.state.cancel_requests.add(transfer_id)
        record.status = "CANCELLING"
        await publish_transfer(record)
        await audit_request(
            request,
            actor_user_id=user.id,
            action="TRANSFER_CANCELLED",
            resource_type="transfer",
            resource_id=str(transfer_id),
            result="SUCCESS",
        )
        return record.public_dict()

    return app


app = create_app()
