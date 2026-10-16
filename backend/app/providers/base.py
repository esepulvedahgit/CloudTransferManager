from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StorageItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    path: str
    is_dir: bool
    size_bytes: int | None = None
    modified_at: datetime | None = None
    mime_type: str | None = None
    provider_type: str
    object_id: str | None = None
    parent_object_id: str | None = None


class PaginatedItems(BaseModel):
    items: list[StorageItem]
    next_cursor: str | None = None
    total_count: int | None = None


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    error_code: str | None = None
    quota_total_bytes: int | None = None
    quota_used_bytes: int | None = None


class StorageQuota(BaseModel):
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None


class StorageProvider(ABC):
    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Check whether the provider is reachable."""

    @abstractmethod
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
        """List files and directories directly below a path."""

    @abstractmethod
    async def get_item(
        self,
        path: str,
        object_id: str | None = None,
        parent_object_id: str | None = None,
    ) -> StorageItem | None:
        """Return metadata for one item."""

    @abstractmethod
    async def get_quota(self) -> StorageQuota | None:
        """Return quota information when supported."""
