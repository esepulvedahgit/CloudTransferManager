import json
import uuid
from datetime import UTC, datetime
from typing import Any

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


class RcloneStorageProvider(StorageProvider):
    provider_type: str

    def __init__(self, connection_id: uuid.UUID, credentials: dict[str, Any], runner: RcloneRunner):
        self.connection_id = connection_id
        self.credentials = credentials
        self.runner = runner
        self.remote_name = f"ctm_{connection_id.hex}"

    def config_content(self) -> str:
        raise NotImplementedError

    def remote_path(self, path: str) -> str:
        normalized = MockStorageProvider.normalize_path(path)
        return f"{self.remote_name}:{normalized}"

    async def test_connection(self) -> ConnectionTestResult:
        try:
            await self.list_items("/", page_size=1)
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
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if cursor is not None and not cursor.isdigit():
            raise ValueError("Invalid cursor")
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        safe_path = MockStorageProvider.normalize_path(path)
        stdout, _stderr = await self.runner.run(
            "lsjson", [self.remote_path(safe_path)], self.config_content(), uuid.uuid4()
        )
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
                    size_bytes=entry.get("Size") if isinstance(entry.get("Size"), int) else None,
                    modified_at=modified_at,
                    mime_type=entry.get("MimeType")
                    if isinstance(entry.get("MimeType"), str)
                    else None,
                    provider_type=self.provider_type,
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

    async def get_item(self, path: str) -> StorageItem | None:
        normalized = MockStorageProvider.normalize_path(path)
        if normalized == "/":
            return StorageItem(name="/", path="/", is_dir=True, provider_type=self.provider_type)
        parent = normalized.rsplit("/", 1)[0] or "/"
        cursor: str | None = None
        while True:
            page = await self.list_items(parent, cursor=cursor, page_size=200)
            match = next((item for item in page.items if item.path == normalized), None)
            if match is not None:
                return match
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    async def get_quota(self) -> StorageQuota | None:
        try:
            stdout, _stderr = await self.runner.run(
                "about", [self.remote_path("/")], self.config_content(), uuid.uuid4()
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
