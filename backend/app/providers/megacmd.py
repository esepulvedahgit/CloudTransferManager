from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.providers.base import (
    ConnectionTestResult,
    PaginatedItems,
    StorageItem,
    StorageProvider,
    StorageQuota,
)
from app.providers.mock import MockStorageProvider
from app.services.megacmd import CancelCheck, LineCallback, MegaCmdError, MegaCmdRunner

_QUOTA_LINE = re.compile(
    r"USED STORAGE:\s*([\d.]+(?:\s*[A-Za-z]+)?)"
    r"(?:\s+[\d.]+%\s+of\s+([\d.]+(?:\s*[A-Za-z]+)?))?",
    re.IGNORECASE,
)
_SIZE = re.compile(r"^([\d.]+)\s*([A-Za-z]+)?$")
_SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1_024,
    "MIB": 1_048_576,
    "GIB": 1_073_741_824,
    "TIB": 1_099_511_627_776,
}


class MegaCmdSession:
    """One authenticated MEGAcmd daemon isolated under a private HOME."""

    _login_timeout_seconds = 30.0
    _metadata_timeout_seconds = 10.0
    _cleanup_timeout_seconds = 5.0

    def __init__(
        self,
        username: str,
        password: str,
        runner: MegaCmdRunner,
        staging_path: Path,
        *,
        transfer_id: uuid.UUID | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.runner = runner
        self.staging_path = staging_path
        self.transfer_id = transfer_id
        self.should_cancel = should_cancel
        self.home: Path | None = None
        self._closed = False
        self._daemon_stopped = False

    async def __aenter__(self) -> MegaCmdSession:
        self.staging_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix = f"megacmd-{self.transfer_id.hex[:12]}-" if self.transfer_id else "megacmd-"
        self.home = Path(tempfile.mkdtemp(prefix=prefix, dir=self.staging_path))
        self.home.chmod(0o700)
        try:
            async with asyncio.timeout(self._login_timeout_seconds + 2):
                await self.runner.login(
                    self.username,
                    self.password,
                    home=self.home,
                    should_cancel=self.should_cancel,
                    timeout_seconds=self._login_timeout_seconds,
                )
        except BaseException:
            await self._close_shielded()
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._close_shielded()

    async def _close_shielded(self) -> None:
        cleanup_task = asyncio.create_task(self.close())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def close(self) -> None:
        if self._closed:
            return
        if self.home is None:
            self._closed = True
            return
        if not self._daemon_stopped:
            try:
                async with asyncio.timeout(self._cleanup_timeout_seconds):
                    await self.runner.run(
                        "logout",
                        [],
                        home=self.home,
                        timeout_seconds=max(0.01, self._cleanup_timeout_seconds - 2),
                    )
            except Exception:
                # Quitting is the safety boundary; logout may fail for an expired session.
                pass
            try:
                async with asyncio.timeout(self._cleanup_timeout_seconds):
                    await self.runner.run(
                        "quit",
                        [],
                        home=self.home,
                        timeout_seconds=max(0.01, self._cleanup_timeout_seconds - 2),
                    )
            except Exception:
                raise MegaCmdError(
                    "No fue posible detener el servidor MEGAcmd; se conserva su HOME privado"
                ) from None
            self._daemon_stopped = True
        await self._remove_home()
        self._closed = True

    async def list_items(
        self,
        path: str,
        cursor: str | None = None,
        page_size: int = 200,
        sort_by: str = "name",
        direction: str = "asc",
        search: str | None = None,
    ) -> PaginatedItems:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if cursor is not None and not cursor.isdigit():
            raise ValueError("Invalid cursor")
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        safe_path = MockStorageProvider.normalize_path(path)
        stdout, _stderr = await self._run(
            "ls", ["-l", "--time-format=ISO6081_WITH_TIME", safe_path]
        )
        items = _parse_listing(stdout, safe_path)
        if search:
            items = [item for item in items if search.casefold() in item.name.casefold()]

        def sort_value(item: StorageItem) -> float | int | str:
            if sort_by == "modified_at":
                if item.modified_at is None:
                    return 0.0
                modified_at = item.modified_at
                if modified_at.tzinfo is None:
                    modified_at = modified_at.replace(tzinfo=UTC)
                return modified_at.timestamp()
            if sort_by == "size":
                return item.size_bytes or 0
            return item.name.casefold()

        directories = [item for item in items if item.is_dir]
        files = [item for item in items if not item.is_dir]
        directories.sort(key=sort_value, reverse=direction == "desc")
        files.sort(key=sort_value, reverse=direction == "desc")
        items = directories + files
        offset = int(cursor or 0)
        page = items[offset : offset + page_size]
        next_offset = offset + page_size
        return PaginatedItems(
            items=page,
            next_cursor=str(next_offset) if next_offset < len(items) else None,
            total_count=len(items),
        )

    async def get_item(self, path: str) -> StorageItem | None:
        normalized = MockStorageProvider.normalize_path(path)
        if normalized == "/":
            return StorageItem(name="/", path="/", is_dir=True, provider_type="MEGA")
        parent, _, name = normalized.rpartition("/")
        result = await self.list_items(parent or "/", page_size=200, search=name)
        return next((item for item in result.items if item.path == normalized), None)

    async def get_quota(self) -> StorageQuota | None:
        stdout, stderr = await self._run("df", ["-h"])
        quota = _parse_quota(stdout or stderr)
        return quota

    async def download_file(
        self,
        remote_path: str,
        destination_dir: Path,
        *,
        line_callback: LineCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> Path:
        safe_remote_path = MockStorageProvider.normalize_path(remote_path)
        if safe_remote_path == "/":
            raise ValueError("A remote file path is required")
        destination = destination_dir.resolve()
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        await self._run(
            "get",
            [safe_remote_path, str(destination)],
            line_callback=line_callback,
            should_cancel=should_cancel,
        )
        local_file = destination / safe_remote_path.rsplit("/", 1)[-1]
        if not local_file.is_file():
            raise RuntimeError("MEGAcmd no creó el archivo descargado esperado")
        return local_file

    async def upload_file(
        self,
        local_file: Path,
        remote_folder: str,
        *,
        line_callback: LineCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        if not local_file.is_absolute() or not local_file.is_file():
            raise ValueError("El archivo local para MEGA debe existir y usar una ruta absoluta")
        safe_remote_folder = MockStorageProvider.normalize_path(remote_folder)
        await self._run(
            "put",
            ["-c", str(local_file.resolve()), safe_remote_folder],
            line_callback=line_callback,
            should_cancel=should_cancel,
        )

    async def mkdir(self, remote_path: str) -> None:
        safe_remote_path = MockStorageProvider.normalize_path(remote_path)
        await self._run("mkdir", ["-p", safe_remote_path])

    async def _run(
        self,
        action: str,
        arguments: list[str],
        *,
        line_callback: LineCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> tuple[bytes, bytes]:
        if self.home is None or self._closed:
            raise RuntimeError("La sesión MEGA no está activa")
        timeout = self._metadata_timeout_seconds if action in {"ls", "df"} else None
        async with asyncio.timeout(timeout + 2 if timeout is not None else None):
            return await self.runner.run(
                action,  # type: ignore[arg-type]
                arguments,
                home=self.home,
                line_callback=line_callback,
                should_cancel=should_cancel or self.should_cancel,
                timeout_seconds=timeout,
            )

    async def _remove_home(self) -> None:
        if self.home is None:
            return
        home = self.home
        try:
            await asyncio.to_thread(shutil.rmtree, home)
        except FileNotFoundError:
            pass
        except OSError:
            raise MegaCmdError("No fue posible retirar el HOME privado de MEGAcmd") from None
        self.home = None


class MegaCmdProvider(StorageProvider):
    provider_type = "MEGA"

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        runner: MegaCmdRunner,
        staging_path: Path,
    ) -> None:
        self.connection_id = connection_id
        self.credentials = credentials
        self.runner = runner
        self.staging_path = Path(staging_path)
        username, password = self.validate_credentials(credentials)
        self._username = username
        self._password = password

    @staticmethod
    def validate_credentials(credentials: dict[str, Any]) -> tuple[str, str]:
        username = credentials.get("username")
        password = credentials.get("password")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("MEGA username is required")
        if not isinstance(password, str) or not password:
            raise ValueError("MEGA password is required")
        for value in (username, password):
            if any(character in value for character in "\x00\r\n"):
                raise ValueError("MEGA credentials contain an invalid character")
            if len(value.encode("utf-8")) > 16 * 1024:
                raise ValueError("MEGA credential is too long")
        return username, password

    @asynccontextmanager
    async def open_session(
        self,
        *,
        transfer_id: uuid.UUID | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> AsyncIterator[MegaCmdSession]:
        session = MegaCmdSession(
            self._username,
            self._password,
            self.runner,
            self.staging_path,
            transfer_id=transfer_id,
            should_cancel=should_cancel,
        )
        async with session:
            yield session

    async def test_connection(self) -> ConnectionTestResult:
        try:
            async with self.open_session() as session:
                await session.list_items("/", page_size=1)
            return ConnectionTestResult(success=True, message="Conexión verificada exitosamente")
        except Exception:
            return ConnectionTestResult(
                success=False,
                message="No fue posible verificar la conexión",
                error_code="PROVIDER_CONNECTION_FAILED",
            )

    async def list_items(
        self,
        path: str,
        cursor: str | None = None,
        page_size: int = 50,
        sort_by: str = "name",
        direction: str = "asc",
        search: str | None = None,
    ) -> PaginatedItems:
        async with self.open_session() as session:
            return await session.list_items(
                path,
                cursor=cursor,
                page_size=page_size,
                sort_by=sort_by,
                direction=direction,
                search=search,
            )

    async def get_item(self, path: str) -> StorageItem | None:
        async with self.open_session() as session:
            return await session.get_item(path)

    async def get_quota(self) -> StorageQuota | None:
        try:
            async with self.open_session() as session:
                return await session.get_quota()
        except Exception:
            return None


def _parse_listing(output: bytes, parent_path: str) -> list[StorageItem]:
    text = output.decode("utf-8", errors="replace")
    items: list[StorageItem] = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        fields = line.lstrip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        flags, _version, raw_size, time_and_name = fields
        # Column titles and absolute directory headings are not nodes. Match
        # their first column, never the filename (a real NAME must survive).
        if flags.upper() == "FLAGS" or flags.startswith("/"):
            continue
        parsed_time = _parse_time_and_name(time_and_name)
        if parsed_time is None:
            continue
        modified_at, name = parsed_time
        if not name.strip() or name in {".", ".."} or "/" in name or "\\" in name:
            continue
        try:
            path = MockStorageProvider.normalize_path(f"{parent_path.rstrip('/')}/{name}")
        except ValueError:
            continue
        try:
            size = int(raw_size) if raw_size.isdigit() else None
        except ValueError:
            size = None
        items.append(
            StorageItem(
                name=name,
                path=path,
                is_dir=flags.lower().startswith("d"),
                size_bytes=size,
                modified_at=modified_at,
                provider_type="MEGA",
            )
        )
    return items


def _parse_time_and_name(value: str) -> tuple[datetime | None, str] | None:
    first, separator, remainder = value.partition(" ")
    if not separator or not remainder:
        return None
    if "T" in first or first.endswith("Z"):
        return _parse_modified_time(first, None), remainder
    second, separator, name = remainder.partition(" ")
    if separator and re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?", second):
        return _parse_modified_time(first, second), name
    # MEGAcmd's legacy date form is e.g. 27Jul2025 16:25:37.
    if re.fullmatch(r"\d{1,2}[A-Za-z]{3}\d{4}", first) and separator:
        return _parse_modified_time(first, second), name
    # ISO date and time may be separated by a space; date-only timestamps are not useful here.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first) and separator:
        return _parse_modified_time(first, second), name
    return _parse_modified_time(first, None), remainder


def _parse_modified_time(first: str, second: str | None) -> datetime | None:
    raw = f"{first} {second}" if second else first
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d%b%Y %H:%M:%S", "%d%b%Y"):
            try:
                return datetime.strptime(raw, pattern)
            except ValueError:
                continue
    return None


def _parse_quota(output: bytes) -> StorageQuota | None:
    text = output.decode("utf-8", errors="replace")
    match = _QUOTA_LINE.search(text)
    if not match:
        return None
    used = _parse_size(match.group(1))
    total = _parse_size(match.group(2)) if match.group(2) else None
    if used is None or total is None:
        return None
    return StorageQuota(total_bytes=total, used_bytes=used, free_bytes=max(total - used, 0))


def _parse_size(value: str) -> int | None:
    match = _SIZE.fullmatch(value.strip())
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "B").upper()
    multiplier = _SIZE_MULTIPLIERS.get(unit)
    return int(amount * multiplier) if multiplier is not None else None
