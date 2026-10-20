from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.providers.base import (
    ConnectionTestResult,
    PaginatedItems,
    StorageItem,
    StorageProvider,
    StorageQuota,
)
from app.providers.seafile_auth import normalize_seafile_base_url

CancelCheck = Callable[[], Awaitable[bool]]
ProgressCallback = Callable[[int, int | None], Awaitable[None]]


class SeafileApiError(RuntimeError):
    _retryable_codes = {408, 429, 500, 502, 503, 504, 520}

    def __init__(
        self,
        status_code: int | None,
        retry_after: int | None = None,
        *,
        safe_message: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.retryable = status_code is None or status_code in self._retryable_codes
        self.retry_after = retry_after
        message = safe_message or "Seafile API request failed"
        if safe_message is None and status_code is not None:
            message += f" (HTTP {status_code})"
        super().__init__(message)


class SeafileApiCancelledError(RuntimeError):
    pass


class SeafileApiProvider(StorageProvider):
    provider_type = "SEAFILE"

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        del connection_id
        base_url = credentials.get("url")
        token = credentials.get("auth_token")
        username = credentials.get("username")
        if not isinstance(base_url, str) or not isinstance(token, str) or not token:
            raise ValueError("Seafile URL and account token are required")
        if not isinstance(username, str) or not username:
            raise ValueError("Seafile username is required")
        if not 64 * 1024 <= chunk_size <= 64 * 1024 * 1024:
            raise ValueError("Seafile upload chunk size is outside the allowed range")
        self.base_url = normalize_seafile_base_url(base_url)
        self.base_parts = urlsplit(self.base_url)
        self.username = username
        self.password = credentials.get("password", "")
        self.token = token
        self.library = credentials.get("library") or None
        self.library_password = credentials.get("library_password") or None
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.chunk_size = chunk_size
        self._libraries: list[dict[str, Any]] | None = None
        self._unlocked_repositories: dict[str, float] = {}

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.timeout_seconds,
                connect=min(10.0, self.timeout_seconds),
                read=self.timeout_seconds,
                write=self.timeout_seconds,
                pool=min(10.0, self.timeout_seconds),
            ),
            transport=self.transport,
            follow_redirects=False,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        headers: dict[str, str] | None = None,
        absolute_url: str | None = None,
    ) -> httpx.Response:
        request_headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        request_headers.update(headers or {})
        url = absolute_url or f"{self.base_url}{path}"
        try:
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
                files=files,
                headers=request_headers,
            )
        except httpx.HTTPError:
            raise SeafileApiError(None) from None
        if not 200 <= response.status_code < 300:
            retry_after = _retry_after(response.headers.get("Retry-After"))
            raise SeafileApiError(response.status_code, retry_after)
        return response

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not isinstance(path, str) or any(character in path for character in "\\\x00\r\n"):
            raise ValueError("Unsafe Seafile path")
        value = path if path.startswith("/") else f"/{path}"
        parts = value.split("/")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("Unsafe Seafile path")
        normalized = str(PurePosixPath(value))
        if normalized == ".":
            return "/"
        return normalized

    async def _libraries_for(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        if self._libraries is None:
            response = await self._request(client, "GET", "/api2/repos/")
            payload = _json_response(response)
            if not isinstance(payload, list):
                raise SeafileApiError(502)
            self._libraries = [
                item
                for item in payload
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("id"), str)
            ]
        return self._libraries

    async def _resolve_library(
        self, client: httpx.AsyncClient, path: str
    ) -> tuple[dict[str, Any], str, str]:
        safe_path = self._normalize_path(path)
        libraries = await self._libraries_for(client)
        if self.library:
            selected = next((item for item in libraries if item["name"] == self.library), None)
            if selected is None:
                raise FileNotFoundError("Seafile library was not found")
            return selected, safe_path, safe_path
        if safe_path == "/":
            raise ValueError("A Seafile library must be selected for this path")
        library_name, _, repository_path = safe_path.lstrip("/").partition("/")
        selected = next((item for item in libraries if item["name"] == library_name), None)
        if selected is None:
            raise FileNotFoundError("Seafile library was not found")
        relative = f"/{repository_path}" if repository_path else "/"
        return selected, relative, safe_path

    async def _library_is_encrypted(
        self, client: httpx.AsyncClient, library: dict[str, Any]
    ) -> bool:
        value = library.get("encrypted")
        if "encrypted" in library:
            return value not in (None, False, 0, "", "0", "false", "False")
        response = await self._request(client, "GET", f"/api2/repos/{library['id']}/")
        info = _json_response(response)
        if not isinstance(info, dict):
            raise SeafileApiError(502)
        return info.get("encrypted") not in (None, False, 0, "", "0", "false", "False") or bool(
            info.get("enc_version")
        )

    async def _ensure_unlocked(self, client: httpx.AsyncClient, library: dict[str, Any]) -> None:
        repository_id = library["id"]
        if self._unlocked_repositories.get(repository_id, 0) > time.monotonic():
            return
        if await self._library_is_encrypted(client, library):
            if not isinstance(self.library_password, str) or not self.library_password:
                raise SeafileApiError(440)
            await self._request(
                client,
                "POST",
                f"/api2/repos/{repository_id}/",
                data={"password": self.library_password},
            )
        self._unlocked_repositories[repository_id] = time.monotonic() + 45 * 60

    async def _directory_entries(
        self,
        client: httpx.AsyncClient,
        library: dict[str, Any],
        repository_path: str,
    ) -> list[dict[str, Any]]:
        await self._ensure_unlocked(client, library)
        response = await self._request(
            client,
            "GET",
            f"/api2/repos/{library['id']}/dir/",
            params={"p": repository_path},
        )
        payload = _json_response(response)
        if not isinstance(payload, list):
            raise SeafileApiError(502)
        return [item for item in payload if isinstance(item, dict)]

    async def test_connection(self) -> ConnectionTestResult:
        try:
            async with await self._client() as client:
                await self._request(client, "GET", "/api2/auth/ping/")
                await self._list_items(client, "/", None, 1, "name", "asc", None)
            return ConnectionTestResult(success=True, message="Conexión verificada exitosamente")
        except SeafileApiError as exc:
            code = (
                "SEAFILE_AUTHENTICATION_FAILED"
                if exc.status_code in {401, 403}
                else "SEAFILE_API_FAILED"
            )
            return ConnectionTestResult(
                success=False,
                message="No fue posible verificar la conexión Seafile",
                error_code=code,
            )
        except (ValueError, FileNotFoundError):
            return ConnectionTestResult(
                success=False,
                message="No fue posible verificar la conexión Seafile",
                error_code="SEAFILE_API_FAILED",
            )

    async def list_items(
        self,
        path: str,
        cursor: str | None = None,
        page_size: int = 50,
        sort_by: str = "name",
        direction: str = "asc",
        search: str | None = None,
        parent_object_id: str | None = None,
    ) -> PaginatedItems:
        del parent_object_id
        async with await self._client() as client:
            return await self._list_items(
                client, path, cursor, page_size, sort_by, direction, search
            )

    async def _list_items(
        self,
        client: httpx.AsyncClient,
        path: str,
        cursor: str | None,
        page_size: int,
        sort_by: str,
        direction: str,
        search: str | None,
    ) -> PaginatedItems:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if cursor is not None and not cursor.isdigit():
            raise ValueError("Invalid cursor")
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        safe_path = self._normalize_path(path)
        if safe_path == "/" and not self.library:
            libraries = await self._libraries_for(client)
            items = [
                StorageItem(
                    name=library["name"],
                    path=self._normalize_path(f"/{library['name']}"),
                    is_dir=True,
                    size_bytes=_integer_or_none(library.get("size")),
                    modified_at=_timestamp(library.get("mtime")),
                    provider_type=self.provider_type,
                )
                for library in libraries
            ]
        else:
            library, repository_path, full_path = await self._resolve_library(client, safe_path)
            entries = await self._directory_entries(client, library, repository_path)
            items = []
            for entry in entries:
                name = entry.get("name")
                entry_type = entry.get("type")
                if not isinstance(name, str) or entry_type not in {"file", "dir"}:
                    continue
                item_path = self._normalize_path(f"{full_path.rstrip('/')}/{name}")
                items.append(
                    StorageItem(
                        name=name,
                        path=item_path,
                        is_dir=entry_type == "dir",
                        size_bytes=_integer_or_none(entry.get("size")),
                        modified_at=_timestamp(entry.get("mtime")),
                        provider_type=self.provider_type,
                    )
                )
        if search:
            items = [item for item in items if search.casefold() in item.name.casefold()]
        items.sort(key=lambda item: (not item.is_dir, _sort_key(item, sort_by)))
        if direction == "desc":
            directories = sorted(
                (item for item in items if item.is_dir),
                key=lambda item: _sort_key(item, sort_by),
                reverse=True,
            )
            files = sorted(
                (item for item in items if not item.is_dir),
                key=lambda item: _sort_key(item, sort_by),
                reverse=True,
            )
            items = directories + files
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
        del object_id
        del parent_object_id
        safe_path = self._normalize_path(path)
        if safe_path == "/":
            return StorageItem(name="/", path="/", is_dir=True, provider_type=self.provider_type)
        parent_path = safe_path.rsplit("/", 1)[0] or "/"
        cursor: str | None = None
        while True:
            page = await self.list_items(parent_path, cursor=cursor, page_size=200)
            found = next((item for item in page.items if item.path == safe_path), None)
            if found is not None:
                return found
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    async def get_quota(self) -> StorageQuota | None:
        return None

    async def mkdir(self, path: str, should_cancel: CancelCheck | None = None) -> None:
        safe_path = self._normalize_path(path)
        await _check_cancel(should_cancel)
        if safe_path == "/":
            return
        async with await self._client() as client:
            library, repository_path, _full_path = await self._resolve_library(client, safe_path)
            if repository_path == "/":
                return
            await self._ensure_unlocked(client, library)
            current = ""
            for part in PurePosixPath(repository_path).parts:
                if part == "/":
                    continue
                await _check_cancel(should_cancel)
                current = f"{current}/{part}"
                parent = current.rsplit("/", 1)[0] or "/"
                entries = await self._directory_entries(client, library, parent)
                existing = next((entry for entry in entries if entry.get("name") == part), None)
                if existing is not None:
                    if existing.get("type") != "dir":
                        raise FileExistsError("A file already exists at the Seafile directory path")
                    continue
                await self._request(
                    client,
                    "POST",
                    f"/api2/repos/{library['id']}/dir/",
                    params={"p": current},
                    data={"operation": "mkdir"},
                )

    async def download_file(
        self,
        path: str,
        target: Path,
        should_cancel: CancelCheck | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        safe_path = self._normalize_path(path)
        await _check_cancel(should_cancel)
        async with await self._client() as client:
            library, repository_path, _full_path = await self._resolve_library(client, safe_path)
            await self._ensure_unlocked(client, library)
            download_url = await self._download_url(client, library, repository_path)
            try:
                async with client.stream(
                    "GET", download_url, headers={"Authorization": f"Token {self.token}"}
                ) as response:
                    self._raise_for_status(response)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    transferred = 0
                    total = self._decoded_content_length(response)
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            await _check_cancel(should_cancel)
                            output.write(chunk)
                            transferred += len(chunk)
                            if progress_callback is not None:
                                await progress_callback(transferred, total)
                    if total is not None and transferred != total:
                        raise SeafileApiError(None, safe_message="Incomplete Seafile download")
            except httpx.HTTPError:
                raise SeafileApiError(None) from None

    async def sha256(
        self,
        path: str,
        should_cancel: CancelCheck | None = None,
    ) -> str:
        safe_path = self._normalize_path(path)
        await _check_cancel(should_cancel)
        digest = hashlib.sha256()
        async with await self._client() as client:
            library, repository_path, _full_path = await self._resolve_library(client, safe_path)
            await self._ensure_unlocked(client, library)
            download_url = await self._download_url(client, library, repository_path)
            try:
                async with client.stream(
                    "GET", download_url, headers={"Authorization": f"Token {self.token}"}
                ) as response:
                    self._raise_for_status(response)
                    transferred = 0
                    total = self._decoded_content_length(response)
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        await _check_cancel(should_cancel)
                        digest.update(chunk)
                        transferred += len(chunk)
                    if total is not None and transferred != total:
                        raise SeafileApiError(None, safe_message="Incomplete Seafile download")
            except httpx.HTTPError:
                raise SeafileApiError(None) from None
        return digest.hexdigest()

    async def _download_url(
        self,
        client: httpx.AsyncClient,
        library: dict[str, Any],
        repository_path: str,
    ) -> str:
        response = await self._request(
            client,
            "GET",
            f"/api2/repos/{library['id']}/file/",
            params={"p": repository_path},
        )
        try:
            payload = response.json()
        except ValueError:
            raise SeafileApiError(response.status_code) from None
        link = payload.get("download_link") if isinstance(payload, dict) else payload
        if not isinstance(link, str) or not link:
            raise SeafileApiError(502)
        return self._validate_transfer_url(link)

    @staticmethod
    def _decoded_content_length(response: httpx.Response) -> int | None:
        """Return Content-Length only when it measures the bytes aiter_bytes yields."""
        content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            return None
        content_length = response.headers.get("Content-Length")
        try:
            return max(0, int(content_length)) if content_length is not None else None
        except ValueError:
            return None

    def _validate_transfer_url(self, value: str) -> str:
        target = urljoin(f"{self.base_url}/", value)
        parsed = urlsplit(target)
        if (
            parsed.scheme != self.base_parts.scheme
            or parsed.hostname != self.base_parts.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise SeafileApiError(400, safe_message="Invalid Seafile transfer URL")
        return target

    async def upload_file(
        self,
        source: Path,
        path: str,
        replace: bool = True,
        should_cancel: CancelCheck | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> str:
        safe_path = self._normalize_path(path)
        await _check_cancel(should_cancel)
        relative = PurePosixPath(safe_path)
        if relative.name in {"", ".", "/"}:
            raise ValueError("Seafile destination must include a file name")
        parent_path = relative.parent.as_posix()
        if not parent_path.startswith("/"):
            parent_path = f"/{parent_path}"
        await self.mkdir(parent_path, should_cancel=should_cancel)
        size = source.stat().st_size
        async with await self._client() as client:
            library, repository_parent, _full_path = await self._resolve_library(
                client, parent_path
            )
            await self._ensure_unlocked(client, library)
            response = await self._request(
                client,
                "GET",
                f"/api/v2.1/repos/{library['id']}/file-uploaded-bytes/",
                params={"parent_dir": repository_parent, "file_name": relative.name},
            )
            payload = _json_response(response)
            offset = payload.get("uploadedBytes") if isinstance(payload, dict) else None
            if not isinstance(offset, int) or offset < 0 or offset > size:
                raise SeafileApiError(400, safe_message="Invalid Seafile upload offset")
            response = await self._request(
                client,
                "GET",
                f"/api2/repos/{library['id']}/upload-link/",
                params={"p": repository_parent},
            )
            try:
                upload_link_payload = response.json()
            except ValueError:
                raise SeafileApiError(response.status_code) from None
            link = (
                upload_link_payload.get("upload_link")
                if isinstance(upload_link_payload, dict)
                else upload_link_payload
            )
            if not isinstance(link, str) or not link:
                raise SeafileApiError(502)
            upload_url = self._validate_transfer_url(link)
            upload_parts = urlsplit(upload_url)
            query = f"{upload_parts.query}&ret-json=1" if upload_parts.query else "ret-json=1"
            upload_url = upload_parts._replace(query=query).geturl()
            # Seafile's chunk upload parser URI-decodes the quoted value from this
            # header. Do not include a plain filename fallback: its parser chooses it.
            encoded_name = quote(relative.name, safe="")
            disposition = f"attachment; filename*=UTF-8''\"{encoded_name}\""
            last_response: httpx.Response | None = None
            with source.open("rb") as file_handle:
                if size == 0:
                    await _check_cancel(should_cancel)
                    last_response = await self._upload_chunk(
                        client,
                        upload_url,
                        file_handle.read(0),
                        repository_parent,
                        relative.name,
                        disposition,
                        replace,
                        None,
                    )
                    if progress_callback is not None:
                        await progress_callback(0, 0)
                while offset < size:
                    await _check_cancel(should_cancel)
                    file_handle.seek(offset)
                    chunk = file_handle.read(min(self.chunk_size, size - offset))
                    if not chunk:
                        raise SeafileApiError(None)
                    end = offset + len(chunk) - 1
                    content_range = f"bytes {offset}-{end}/{size}"
                    last_response = await self._upload_chunk(
                        client,
                        upload_url,
                        chunk,
                        repository_parent,
                        relative.name,
                        disposition,
                        replace,
                        content_range,
                    )
                    offset = end + 1
                    if progress_callback is not None:
                        await progress_callback(offset, size)
            actual_name = (
                relative.name
                if last_response is None
                else self._uploaded_name(last_response)
            )
            parent = safe_path.rsplit("/", 1)[0] or "/"
            return self._normalize_path(f"{parent.rstrip('/')}/{actual_name}")

    async def _upload_chunk(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        chunk: bytes,
        parent_dir: str,
        file_name: str,
        content_disposition: str,
        replace: bool,
        content_range: str | None,
    ) -> httpx.Response:
        headers = {"Content-Disposition": content_disposition}
        if content_range:
            headers["Content-Range"] = content_range
        return await self._request(
            client,
            "POST",
            "",
            absolute_url=upload_url,
            data={"parent_dir": parent_dir, "replace": "1" if replace else "0"},
            files={"file": (file_name, chunk, "application/octet-stream")},
            headers=headers,
        )

    @staticmethod
    def _uploaded_name(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            raise SeafileApiError(502, safe_message="Invalid Seafile upload response") from None
        if not isinstance(payload, list) or len(payload) != 1:
            raise SeafileApiError(502, safe_message="Invalid Seafile upload response")
        result = payload[0]
        actual_name = result.get("name") if isinstance(result, dict) else None
        if not isinstance(actual_name, str) or not actual_name or "/" in actual_name:
            raise SeafileApiError(502, safe_message="Invalid Seafile upload response")
        return actual_name

    def _raise_for_status(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise SeafileApiError(
                response.status_code, _retry_after(response.headers.get("Retry-After"))
            )


def _json_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise SeafileApiError(response.status_code) from None


def _integer_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _sort_key(item: StorageItem, sort_by: str) -> Any:
    if sort_by == "size":
        return item.size_bytes or 0
    if sort_by == "modified_at":
        return item.modified_at.timestamp() if item.modified_at else 0
    return item.name.casefold()


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(int(value), 3600))
    except ValueError:
        return None


async def _check_cancel(should_cancel: CancelCheck | None) -> None:
    if should_cancel is not None and await should_cancel():
        raise SeafileApiCancelledError("Seafile transfer was cancelled")
