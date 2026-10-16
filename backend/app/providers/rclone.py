import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.providers.base import (
    ConnectionTestResult,
    PaginatedItems,
    StorageItem,
    StorageProvider,
    StorageQuota,
)
from app.providers.mock import MockStorageProvider
from app.services.rclone import RcloneRunner


def _config_value(value: str, field_name: str) -> str:
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"Invalid character in {field_name}")
    return value


def _google_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


class GoogleDriveApiError(RuntimeError):
    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        self.retryable = status_code is None or status_code in {408, 429, 500, 502, 503, 504}
        message = "Google Drive API request failed"
        if status_code is not None:
            message += f" (HTTP {status_code})"
        super().__init__(message)


class GoogleDriveCancelledError(RuntimeError):
    pass


class RcloneStorageProvider(StorageProvider):
    provider_type: str

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        runner: RcloneRunner,
        *,
        operation_timeout_seconds: float | None = None,
        listing_cache: Any | None = None,
        cache_version: int = 1,
    ):
        self.connection_id = connection_id
        self.credentials = credentials
        self.runner = runner
        self.remote_name = f"ctm_{connection_id.hex}"
        self.operation_timeout_seconds = operation_timeout_seconds
        self.listing_cache = listing_cache
        self.cache_version = cache_version

    def config_content(self) -> str:
        raise NotImplementedError

    def remote_path(self, path: str) -> str:
        normalized = MockStorageProvider.normalize_path(path)
        return f"{self.remote_name}:{normalized}"

    async def test_connection(self) -> ConnectionTestResult:
        try:
            await self.list_items("/", page_size=1, force_refresh=True)
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
        force_refresh: bool = False,
        parent_object_id: str | None = None,
    ) -> PaginatedItems:
        del parent_object_id
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if cursor is not None and not cursor.isdigit():
            raise ValueError("Invalid cursor")
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        safe_path = MockStorageProvider.normalize_path(path)
        cache_key = (self.connection_id, self.cache_version, safe_path)
        stdout = (
            self.listing_cache.get(cache_key)
            if self.listing_cache is not None and not force_refresh
            else None
        )
        if stdout is None:
            options: dict[str, Any] = {}
            if self.operation_timeout_seconds is not None:
                options["timeout_seconds"] = self.operation_timeout_seconds
            stdout, _stderr = await self.runner.run(
                "lsjson",
                [self.remote_path(safe_path)],
                self.config_content(),
                uuid.uuid4(),
                **options,
            )
            if self.listing_cache is not None:
                self.listing_cache.put(cache_key, stdout)
        entries = json.loads(stdout)
        if not isinstance(entries, list):
            raise ValueError("Rclone returned an invalid item list")
        items: list[StorageItem] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("Name"), str):
                continue
            name = entry["Name"]
            item_path = MockStorageProvider.normalize_path(f"{safe_path.rstrip('/')}/{name}")
            modified = entry.get("ModTime")
            try:
                modified_at = (
                    datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None
                )
            except (AttributeError, ValueError):
                modified_at = None
            items.append(
                StorageItem(
                    name=name,
                    path=item_path,
                    is_dir=bool(entry.get("IsDir", False)),
                    size_bytes=entry.get("Size")
                    if isinstance(entry.get("Size"), int) and entry["Size"] >= 0 else None,
                    modified_at=modified_at,
                    mime_type=entry.get("MimeType")
                    if isinstance(entry.get("MimeType"), str)
                    else None,
                    provider_type=self.provider_type,
                    object_id=entry.get("ID") if isinstance(entry.get("ID"), str) else None,
                    parent_object_id=(
                        entry.get("ParentID")
                        if isinstance(entry.get("ParentID"), str)
                        else None
                    ),
                )
            )
        if search:
            items = [item for item in items if search.casefold() in item.name.casefold()]

        def sort_value(item: StorageItem) -> float | int | str:
            if sort_by == "modified_at":
                modified_at = item.modified_at
                if modified_at is None:
                    return 0.0
                if modified_at.tzinfo is None:
                    return modified_at.replace(tzinfo=UTC).timestamp()
                return modified_at.timestamp()
            if sort_by == "size":
                return item.size_bytes or 0
            return item.name.casefold()

        items.sort(key=lambda item: (item.is_dir is False, sort_value(item)))
        if direction == "desc":
            directories = [item for item in items if item.is_dir]
            files = [item for item in items if not item.is_dir]
            items = sorted(directories, key=sort_value, reverse=True)
            items.extend(sorted(files, key=sort_value, reverse=True))
        offset = int(cursor or 0)
        page = items[offset : offset + page_size]
        next_offset = offset + page_size
        return PaginatedItems(
            items=page,
            next_cursor=str(next_offset) if next_offset < len(items) else None,
            total_count=len(items),
        )

    async def get_item(
        self,
        path: str,
        object_id: str | None = None,
        parent_object_id: str | None = None,
    ) -> StorageItem | None:
        del parent_object_id
        normalized = MockStorageProvider.normalize_path(path)
        if normalized == "/":
            return StorageItem(name="/", path="/", is_dir=True, provider_type=self.provider_type)
        parent = normalized.rsplit("/", 1)[0] or "/"
        cursor: str | None = None
        while True:
            page = await self.list_items(parent, cursor=cursor, page_size=200)
            match = next(
                (
                    item
                    for item in page.items
                    if item.path == normalized
                    and (object_id is None or item.object_id == object_id)
                ),
                None,
            )
            if match is not None:
                return match
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    async def get_quota(self) -> StorageQuota | None:
        try:
            options: dict[str, Any] = {}
            if self.operation_timeout_seconds is not None:
                options["timeout_seconds"] = self.operation_timeout_seconds
            stdout, _stderr = await self.runner.run(
                "about", [self.remote_path("/")], self.config_content(), uuid.uuid4(), **options
            )
        except Exception:
            return None
        result = json.loads(stdout)
        total = result.get("total")
        used = result.get("used")
        free = result.get("free")
        return StorageQuota(
            total_bytes=total if isinstance(total, int) else None,
            used_bytes=used if isinstance(used, int) else None,
            free_bytes=free if isinstance(free, int) else None,
        )


class RcloneGoogleDriveProvider(RcloneStorageProvider):
    provider_type = "GOOGLE_DRIVE"

    _file_id_pattern = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
    _export_mime_types = {
        "application/vnd.google-apps.document": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "application/vnd.google-apps.spreadsheet": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "application/vnd.google-apps.presentation": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        "application/vnd.google-apps.drawing": "application/pdf",
    }

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        runner: RcloneRunner,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(connection_id, credentials, runner)
        self.http_transport = http_transport
        self._api_access_token: str | None = None
        self._api_access_token_expires_at = 0.0

    async def list_items(
        self,
        path: str,
        cursor: str | None = None,
        page_size: int = 50,
        sort_by: str = "name",
        direction: str = "asc",
        search: str | None = None,
        force_refresh: bool = False,
        parent_object_id: str | None = None,
    ) -> PaginatedItems:
        if parent_object_id is None:
            return await super().list_items(
                path,
                cursor,
                page_size,
                sort_by,
                direction,
                search,
                force_refresh,
            )
        if not self._file_id_pattern.fullmatch(parent_object_id):
            raise ValueError("Invalid Google Drive folder ID")
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        if cursor is not None and (len(cursor) > 1024 or any(ord(char) < 32 for char in cursor)):
            raise ValueError("Invalid cursor")

        token = await self._google_access_token()
        params: dict[str, str] = {
            "q": f"'{parent_object_id}' in parents and trashed = false",
            "pageSize": str(page_size),
            "fields": (
                "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents)"
            ),
            "orderBy": "name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if cursor:
            params["pageToken"] = cursor
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                transport=self.http_transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            raise GoogleDriveApiError(None) from None
        if not 200 <= response.status_code < 300:
            raise GoogleDriveApiError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            raise GoogleDriveApiError(502)
        safe_parent = MockStorageProvider.normalize_path(path)
        items: list[StorageItem] = []
        for entry in payload["files"]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            object_id = entry.get("id")
            mime_type = entry.get("mimeType")
            if not isinstance(name, str) or not isinstance(object_id, str):
                continue
            item_path = MockStorageProvider.normalize_path(f"{safe_parent.rstrip('/')}/{name}")
            size = _google_size(entry.get("size"))
            modified = entry.get("modifiedTime")
            try:
                modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                modified_at = None
            items.append(
                StorageItem(
                    name=name,
                    path=item_path,
                    is_dir=mime_type == "application/vnd.google-apps.folder",
                    size_bytes=size,
                    modified_at=modified_at,
                    mime_type=mime_type if isinstance(mime_type, str) else None,
                    provider_type=self.provider_type,
                    object_id=object_id,
                    parent_object_id=parent_object_id,
                )
            )
        if search:
            items = [item for item in items if search.casefold() in item.name.casefold()]
        if sort_by == "size":
            sort_key = lambda item: item.size_bytes or 0
        elif sort_by == "modified_at":
            sort_key = lambda item: item.modified_at.timestamp() if item.modified_at else 0.0
        else:
            sort_key = lambda item: item.name.casefold()
        directories = sorted(
            (item for item in items if item.is_dir),
            key=sort_key,
            reverse=direction == "desc",
        )
        files = sorted(
            (item for item in items if not item.is_dir),
            key=sort_key,
            reverse=direction == "desc",
        )
        next_page_token = payload.get("nextPageToken")
        return PaginatedItems(
            items=directories + files,
            next_cursor=next_page_token if isinstance(next_page_token, str) else None,
            total_count=None,
        )

    async def get_item(
        self,
        path: str,
        object_id: str | None = None,
        parent_object_id: str | None = None,
    ) -> StorageItem | None:
        if object_id is None:
            return await super().get_item(path)
        entry = await self._google_file_metadata(object_id)
        if entry is None:
            return None
        name = entry.get("name")
        if not isinstance(name, str):
            raise GoogleDriveApiError(502)
        safe_path = MockStorageProvider.normalize_path(path)
        parent_path = safe_path.rsplit("/", 1)[0] or "/"
        actual_path = MockStorageProvider.normalize_path(f"{parent_path.rstrip('/')}/{name}")
        mime_type = entry.get("mimeType")
        modified = entry.get("modifiedTime")
        try:
            modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            modified_at = None
        parents = entry.get("parents")
        parent_id = parents[0] if isinstance(parents, list) and parents else None
        if parent_object_id is not None and (
            not isinstance(parents, list) or parent_object_id not in parents
        ):
            return None
        return StorageItem(
            name=name,
            path=actual_path,
            is_dir=mime_type == "application/vnd.google-apps.folder",
            size_bytes=_google_size(entry.get("size")),
            modified_at=modified_at,
            mime_type=mime_type if isinstance(mime_type, str) else None,
            provider_type=self.provider_type,
            object_id=object_id,
            parent_object_id=parent_id if isinstance(parent_id, str) else None,
        )

    async def download_file_by_id(
        self,
        item: StorageItem,
        target: Path,
        *,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None = None,
        max_bytes: int | None = None,
    ) -> None:
        object_id = item.object_id
        if not isinstance(object_id, str) or not self._file_id_pattern.fullmatch(object_id):
            raise ValueError("A valid Google Drive file ID is required")
        metadata = await self._google_file_metadata(object_id, include_download_capability=True)
        if metadata is None:
            raise GoogleDriveApiError(404)
        capabilities = metadata.get("capabilities")
        if isinstance(capabilities, dict) and capabilities.get("canDownload") is False:
            raise GoogleDriveApiError(403)
        mime_type = metadata.get("mimeType")
        if mime_type == "application/vnd.google-apps.video":
            raise GoogleDriveApiError(415)
        is_export = isinstance(mime_type, str) and mime_type.startswith(
            "application/vnd.google-apps."
        )
        if is_export:
            export_type = self._export_mime_types.get(mime_type)
            if export_type is None:
                raise GoogleDriveApiError(415)
            request_url = (
                f"https://www.googleapis.com/drive/v3/files/{object_id}/export"
            )
            params = {"mimeType": export_type}
        else:
            request_url = f"https://www.googleapis.com/drive/v3/files/{object_id}"
            params = {"alt": "media", "supportsAllDrives": "true"}
        token = await self._google_access_token()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                transport=self.http_transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET",
                    request_url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise GoogleDriveApiError(response.status_code)
                    total = _google_size(response.headers.get("Content-Length"))
                    if max_bytes is not None and total is not None and total > max_bytes:
                        raise GoogleDriveApiError(413)
                    transferred = 0
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if should_cancel is not None and await should_cancel():
                                raise GoogleDriveCancelledError("Transfer cancelled")
                            if max_bytes is not None and transferred + len(chunk) > max_bytes:
                                raise GoogleDriveApiError(413)
                            output.write(chunk)
                            transferred += len(chunk)
                            if progress_callback is not None:
                                await progress_callback(transferred, total)
                    if total is not None and total != transferred:
                        raise GoogleDriveApiError(None)
        except httpx.HTTPError:
            raise GoogleDriveApiError(None) from None

    async def _google_file_metadata(
        self, object_id: str, *, include_download_capability: bool = False
    ) -> dict[str, Any] | None:
        if not isinstance(object_id, str) or not self._file_id_pattern.fullmatch(object_id):
            raise ValueError("Invalid Google Drive file ID")
        token = await self._google_access_token()
        fields = "id,name,mimeType,size,modifiedTime,parents"
        if include_download_capability:
            fields += ",capabilities(canDownload)"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                transport=self.http_transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{object_id}",
                    params={"fields": fields, "supportsAllDrives": "true"},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            raise GoogleDriveApiError(None) from None
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise GoogleDriveApiError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleDriveApiError(502)
        return payload

    async def _google_access_token(self) -> str:
        import time

        if self._api_access_token and self._api_access_token_expires_at > time.monotonic() + 60:
            return self._api_access_token
        client_id = self.credentials.get("client_id")
        client_secret = self.credentials.get("client_secret")
        refresh_token = self.credentials.get("refresh_token")
        if not all(
            isinstance(value, str) and value
            for value in (client_id, client_secret, refresh_token)
        ):
            raise GoogleDriveApiError(401)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                transport=self.http_transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
        except httpx.HTTPError:
            raise GoogleDriveApiError(None) from None
        if not 200 <= response.status_code < 300:
            raise GoogleDriveApiError(response.status_code)
        payload = response.json()
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise GoogleDriveApiError(502)
        try:
            lifetime = max(60, min(int(expires_in), 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        self._api_access_token = access_token
        self._api_access_token_expires_at = time.monotonic() + lifetime
        return access_token

    def config_content(self) -> str:
        client_id = self.credentials.get("client_id")
        client_secret = self.credentials.get("client_secret")
        refresh_token = self.credentials.get("refresh_token")
        if not all(
            isinstance(value, str) and value for value in (client_id, client_secret, refresh_token)
        ):
            raise ValueError("Google Drive client and refresh credentials are required")
        assert isinstance(client_id, str)
        assert isinstance(client_secret, str)
        assert isinstance(refresh_token, str)
        token = {
            "access_token": self.credentials.get("access_token", ""),
            "token_type": "Bearer",
            "refresh_token": refresh_token,
            "expiry": self.credentials.get("expiry", "1970-01-01T00:00:00Z"),
        }
        lines = [
            f"[{self.remote_name}]",
            "type = drive",
            f"client_id = {_config_value(client_id, 'client_id')}",
            f"client_secret = {_config_value(client_secret, 'client_secret')}",
            f"token = {json.dumps(token, separators=(',', ':'))}",
        ]
        team_drive = self.credentials.get("team_drive")
        if team_drive:
            lines.append(f"team_drive = {_config_value(str(team_drive), 'team_drive')}")
        root_folder_id = self.credentials.get("root_folder_id")
        if root_folder_id:
            lines.append(f"root_folder_id = {_config_value(str(root_folder_id), 'root_folder_id')}")
        return "\n".join(lines) + "\n"
