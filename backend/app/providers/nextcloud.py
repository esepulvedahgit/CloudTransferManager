from __future__ import annotations

import uuid
from collections import OrderedDict
from threading import RLock
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.providers.base import StorageItem
from app.providers.rclone import RcloneStorageProvider, _config_value
from app.providers.seafile import _obscure_for_rclone
from app.services.rclone import RcloneCommandError, RcloneRunner


class _DirectoryListingCache:
    """Small process-local cache of credential-free Rclone listing output."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30,
        max_entry_bytes: int = 4 * 1024 * 1024,
        max_bytes: int = 32 * 1024 * 1024,
        max_entries: int = 512,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entry_bytes = max_entry_bytes
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._lock = RLock()
        self._entries: OrderedDict[
            tuple[uuid.UUID, int, str], tuple[float, bytes]
        ] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple[uuid.UUID, int, str]) -> bytes | None:
        import time

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                self._bytes -= len(value)
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: tuple[uuid.UUID, int, str], value: bytes) -> None:
        import time

        if len(value) > self.max_entry_bytes or len(value) > self.max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous[1])
            while self._entries and (
                self._bytes + len(value) > self.max_bytes
                or len(self._entries) >= self.max_entries
            ):
                _old_key, (_expires_at, old_value) = self._entries.popitem(last=False)
                self._bytes -= len(old_value)
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
            self._bytes += len(value)


_LISTING_CACHE = _DirectoryListingCache()


class NextcloudRcloneProvider(RcloneStorageProvider):
    provider_type = "NEXTCLOUD"
    _required_credentials = {"url", "username", "password"}
    _supported_chunk_sizes = {"10Mi", "32Mi", "64Mi", "128Mi", "256Mi", "512Mi", "1Gi"}

    def __init__(
        self,
        connection_id: uuid.UUID,
        credentials: dict[str, Any],
        runner: RcloneRunner,
        *,
        credential_version: int = 1,
        chunk_size: str = "10Mi",
    ) -> None:
        self.credential_version = credential_version
        if chunk_size not in self._supported_chunk_sizes:
            raise ValueError("Unsupported Nextcloud upload chunk size")
        self.chunk_size = chunk_size
        validated = self.validate_credentials(credentials)
        super().__init__(
            connection_id,
            validated,
            runner,
            operation_timeout_seconds=30,
            listing_cache=_LISTING_CACHE,
            cache_version=credential_version,
        )

    @classmethod
    def validate_credentials(
        cls,
        credentials: dict[str, Any],
    ) -> dict[str, str]:
        if set(credentials) != cls._required_credentials:
            raise ValueError("Nextcloud requires a server URL, username, and app password")
        url = credentials.get("url")
        username = credentials.get("username")
        password = credentials.get("password")
        if not all(isinstance(value, str) and value.strip() for value in (url, username, password)):
            raise ValueError("Nextcloud requires a server URL, username, and app password")
        assert isinstance(url, str)
        assert isinstance(username, str)
        assert isinstance(password, str)
        if any(char in value for value in (url, username, password) for char in "\r\n\x00"):
            raise ValueError("Invalid Nextcloud credential value")
        normalized_username = username.strip()
        if (
            not normalized_username
            or normalized_username in {".", ".."}
            or any(char in normalized_username for char in "/\\")
        ):
            raise ValueError("Invalid Nextcloud username")
        try:
            parsed = urlsplit(url.strip())
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid Nextcloud server URL") from exc
        path_segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(segment in {".", ".."} for segment in path_segments)
            or any("/" in segment or "\\" in segment for segment in path_segments)
        ):
            raise ValueError("Nextcloud URL must be a valid HTTPS server URL")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid Nextcloud server URL")

        legacy_dav_suffix = ["remote.php", "dav", "files", normalized_username]
        if "remote.php" in path_segments:
            if path_segments[-4:] != legacy_dav_suffix:
                raise ValueError("Nextcloud DAV URL does not match the configured user")
            dav_path = parsed.path.rstrip("/") + "/"
        else:
            base_path = parsed.path.rstrip("/")
            encoded_username = quote(normalized_username, safe="")
            dav_path = f"{base_path}/remote.php/dav/files/{encoded_username}/"
        normalized_url = urlunsplit(("https", parsed.netloc, dav_path, "", ""))
        return {"url": normalized_url, "username": normalized_username, "password": password}

    def config_content(self) -> str:
        credentials = self.validate_credentials(self.credentials)
        return (
            "\n".join(
                (
                    f"[{self.remote_name}]",
                    "type = webdav",
                    f"url = {_config_value(credentials['url'], 'url')}",
                    "vendor = nextcloud",
                    f"nextcloud_chunk_size = {self.chunk_size}",
                    f"user = {_config_value(credentials['username'], 'username')}",
                    f"pass = {_obscure_for_rclone(credentials['password'])}",
                )
            )
            + "\n"
        )

    async def get_item(
        self,
        path: str,
        object_id: str | None = None,
        parent_object_id: str | None = None,
    ) -> StorageItem | None:
        del object_id
        del parent_object_id
        from app.providers.mock import MockStorageProvider

        normalized = MockStorageProvider.normalize_path(path)
        if normalized == "/":
            return StorageItem(name="/", path="/", is_dir=True, provider_type=self.provider_type)
        try:
            stdout, _stderr = await self.runner.run(
                "lsjson",
                [self.remote_path(normalized)],
                self.config_content(),
                uuid.uuid4(),
                stat=True,
                timeout_seconds=self.operation_timeout_seconds,
            )
        except RcloneCommandError as exc:
            diagnostic = exc._stderr.decode("utf-8", errors="ignore").casefold()
            if any(marker in diagnostic for marker in (
                "404", "object not found", "directory not found", "file not found"
            )):
                return None
            raise
        import json

        entry = json.loads(stdout)
        if not isinstance(entry, dict):
            raise ValueError("Rclone returned invalid Nextcloud item metadata")
        name = entry.get("Name")
        if not isinstance(name, str):
            name = normalized.rsplit("/", 1)[-1]
        modified = entry.get("ModTime")
        modified_at = None
        if isinstance(modified, str):
            from datetime import datetime

            try:
                modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            except ValueError:
                pass
        return StorageItem(
            name=name,
            path=normalized,
            is_dir=bool(entry.get("IsDir", False)),
            size_bytes=entry.get("Size") if isinstance(entry.get("Size"), int) else None,
            modified_at=modified_at,
            mime_type=entry.get("MimeType") if isinstance(entry.get("MimeType"), str) else None,
            provider_type=self.provider_type,
        )
