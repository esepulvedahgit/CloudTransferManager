from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path


class StagingSpaceError(OSError):
    """Raised when a staging copy would violate its free-space reserve."""


def cleanup_orphaned_transfer_staging_areas(
    root: Path,
    *,
    active_transfer_ids: Collection[uuid.UUID],
    minimum_age_seconds: int = 600,
    now: float | None = None,
) -> int:
    """Remove old transfer temp folders without following links or touching MEGAcmd homes."""
    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds cannot be negative")
    staging_root = Path(root).absolute()
    try:
        _reject_symlink_components(staging_root)
        root_mode = staging_root.lstat().st_mode
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(root_mode):
        raise ValueError("El directorio de staging no es una carpeta")

    staging_root = staging_root.resolve(strict=True)
    active_ids = set(active_transfer_ids)
    current_time = time.time() if now is None else now
    removed = 0
    with os.scandir(staging_root) as entries:
        for entry in entries:
            if not entry.name.startswith("transfer-"):
                continue
            transfer_hex = entry.name.removeprefix("transfer-").split("-", 1)[0]
            if len(transfer_hex) != 32:
                continue
            try:
                transfer_id = uuid.UUID(hex=transfer_hex)
            except ValueError:
                continue
            if transfer_id in active_ids:
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue
            if current_time - entry_stat.st_mtime < minimum_age_seconds:
                continue
            candidate = staging_root / entry.name
            try:
                candidate_stat = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(candidate_stat.st_mode):
                continue
            try:
                shutil.rmtree(candidate)
            except FileNotFoundError:
                continue
            removed += 1
    return removed


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
