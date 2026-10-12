from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class StagingSpaceError(OSError):
    """Raised when a staging copy would violate its free-space reserve."""


@asynccontextmanager
async def temporary_staging_area(
    root: Path,
    *,
    transfer_id: uuid.UUID,
    expected_size: int | None,
    reserve_bytes: int = 0,
    margin_percent: int = 0,
) -> AsyncIterator[Path]:
    if expected_size is not None and expected_size < 0:
        raise ValueError("expected_size cannot be negative")
    if reserve_bytes < 0 or margin_percent < 0:
        raise ValueError("staging reserve and margin cannot be negative")
    staging_root = Path(root).absolute()
    _reject_symlink_components(staging_root)
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_components(staging_root)
    staging_root = staging_root.resolve(strict=True)
    if not staging_root.is_dir():
        raise ValueError("El directorio de staging no es una carpeta")
    margin_bytes = (
        (expected_size * margin_percent + 99) // 100 if expected_size is not None else 0
    )
    required_space = (
        expected_size + max(reserve_bytes, margin_bytes)
        if expected_size is not None
        else 0
    )
    if expected_size is not None and shutil.disk_usage(staging_root).free < required_space:
        raise StagingSpaceError("No hay suficiente espacio disponible para staging")

    area = Path(tempfile.mkdtemp(prefix=f"transfer-{transfer_id.hex}-", dir=staging_root))
    try:
        os.chmod(area, 0o700)
        yield area
    finally:
        cleanup_task = asyncio.create_task(asyncio.to_thread(shutil.rmtree, area))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("El directorio de staging no puede contener enlaces simbólicos")
