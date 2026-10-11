import asyncio
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.providers.base import (
    ConnectionTestResult,
    PaginatedItems,
    StorageItem,
    StorageProvider,
    StorageQuota,
)


class MockStorageProvider(StorageProvider):
    """Deterministic in-memory provider used by the vertical slice and tests."""

    _tree: dict[str, tuple[bool, int | None, str | None]] = {
        "/Documentos": (True, None, None),
        "/Documentos/manual.txt": (False, 2048, "text/plain"),
        "/Proyectos": (True, None, None),
        "/Proyectos/informe.pdf": (False, 1048576, "application/pdf"),
        "/Proyectos/video_demo.mp4": (False, 104857600, "video/mp4"),
    }

    def __init__(self, latency_seconds: float = 0.0) -> None:
        self.latency_seconds = latency_seconds

    async def _wait(self) -> None:
        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

    @staticmethod
    def normalize_path(path: str) -> str:
        if "\\" in path or "\x00" in path:
            raise ValueError("Invalid path")
        parts = path.split("/")
        if any(part == ".." for part in parts):
            raise ValueError("Path traversal is not allowed")
        normalized = str(PurePosixPath("/", path.lstrip("/")))
        if normalized != "/" and any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError("Invalid path")
        return normalized

    async def test_connection(self) -> ConnectionTestResult:
        await self._wait()
        return ConnectionTestResult(
            success=True,
            message="Conexión mock verificada exitosamente",
            quota_total_bytes=10 * 1024**3,
            quota_used_bytes=2 * 1024**3,
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
        await self._wait()
        parent = self.normalize_path(path)
        if page_size < 1 or page_size > 200:
            raise ValueError("page_size must be between 1 and 200")
        if cursor is not None and not cursor.isdigit():
            raise ValueError("Invalid cursor")
        offset = int(cursor or 0)
        children: list[StorageItem] = []
        prefix = "/" if parent == "/" else f"{parent}/"
        for item_path, (is_dir, size_bytes, mime_type) in self._tree.items():
            if not item_path.startswith(prefix):
                continue
            suffix = item_path[len(prefix) :]
            if not suffix or "/" in suffix:
                continue
            name = suffix
            if search and search.casefold() not in name.casefold():
                continue
            children.append(
                StorageItem(
                    name=name,
                    path=item_path,
                    is_dir=is_dir,
                    size_bytes=size_bytes,
                    modified_at=datetime(2026, 10, 1, tzinfo=UTC),
                    mime_type=mime_type,
                    provider_type="MOCK",
                )
            )
        if sort_by not in {"name", "size", "modified_at"}:
            raise ValueError("Unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("Unsupported sort direction")
        children.sort(
            key=lambda item: (
                item.is_dir is False,
                getattr(
                    item, {"size": "size_bytes", "modified_at": "modified_at"}.get(sort_by, "name")
                )
                or 0,
            ),
            reverse=direction == "desc",
        )
        page = children[offset : offset + page_size]
        next_offset = offset + page_size
        return PaginatedItems(
            items=page,
            next_cursor=str(next_offset) if next_offset < len(children) else None,
            total_count=len(children),
        )

    async def get_item(self, path: str) -> StorageItem | None:
        await self._wait()
        item_path = self.normalize_path(path)
        entry = self._tree.get(item_path)
        if entry is None:
            return None
        is_dir, size_bytes, mime_type = entry
        return StorageItem(
            name=PurePosixPath(item_path).name,
            path=item_path,
            is_dir=is_dir,
            size_bytes=size_bytes,
            modified_at=datetime(2026, 10, 1, tzinfo=UTC),
            mime_type=mime_type,
            provider_type="MOCK",
        )

    async def get_quota(self) -> StorageQuota:
        await self._wait()
        return StorageQuota(
            total_bytes=10 * 1024**3, used_bytes=2 * 1024**3, free_bytes=8 * 1024**3
        )
