import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.staging import (
    cleanup_orphaned_transfer_staging_areas,
    temporary_staging_area,
)


@pytest.mark.asyncio
async def test_staging_area_is_private_and_removed_after_normal_exit(tmp_path: Path):
    transfer_id = uuid.uuid4()

    async with temporary_staging_area(tmp_path, transfer_id=transfer_id, expected_size=10) as area:
        assert area.parent == tmp_path
        assert area.exists()
        assert area.stat().st_mode & 0o777 == 0o700
        (area / "partial.bin").write_bytes(b"partial")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_area_is_removed_after_exception(tmp_path: Path):
    with pytest.raises(RuntimeError, match="copy failed"):
        async with temporary_staging_area(
            tmp_path, transfer_id=uuid.uuid4(), expected_size=None
        ) as area:
            (area / "partial.bin").write_bytes(b"partial")
            raise RuntimeError("copy failed")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_area_is_removed_after_cancellation(tmp_path: Path):
    with pytest.raises(asyncio.CancelledError):
        async with temporary_staging_area(
            tmp_path, transfer_id=uuid.uuid4(), expected_size=None
        ) as area:
            (area / "partial.bin").write_bytes(b"partial")
            raise asyncio.CancelledError

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_insufficient_free_space_fails_before_yielding(tmp_path: Path, monkeypatch):
    usage = SimpleNamespace(total=100, used=95, free=5)
    monkeypatch.setattr("app.services.staging.shutil.disk_usage", lambda _path: usage)

    with pytest.raises(OSError, match="espacio disponible"):
        async with temporary_staging_area(
            tmp_path, transfer_id=uuid.uuid4(), expected_size=6
        ):
            pytest.fail("staging should not be yielded without enough free space")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_checks_file_size_plus_configured_reserve(tmp_path: Path, monkeypatch):
    usage = SimpleNamespace(total=200, used=91, free=109)
    monkeypatch.setattr("app.services.staging.shutil.disk_usage", lambda _path: usage)

    with pytest.raises(OSError, match="espacio disponible"):
        async with temporary_staging_area(
            tmp_path,
            transfer_id=uuid.uuid4(),
            expected_size=10,
            reserve_bytes=100,
            margin_percent=50,
        ):
            pytest.fail("staging should keep the configured free-space reserve")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_rejects_symlink_before_creating_directory_outside_root(tmp_path: Path):
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(ValueError, match="enlaces simbólicos"):
        async with temporary_staging_area(
            linked_root / "staging",
            transfer_id=uuid.uuid4(),
            expected_size=None,
        ):
            pytest.fail("staging must reject symlinked roots")

    assert not (actual_root / "staging").exists()


@pytest.mark.asyncio
async def test_staging_area_is_removed_if_private_permissions_cannot_be_set(
    tmp_path: Path, monkeypatch
):
    def fail_chmod(_path, _mode):
        raise OSError("chmod failed")

    monkeypatch.setattr("app.services.staging.os.chmod", fail_chmod)

    with pytest.raises(OSError, match="chmod failed"):
        async with temporary_staging_area(
            tmp_path, transfer_id=uuid.uuid4(), expected_size=None
        ):
            pytest.fail("staging should not be yielded without private permissions")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_cleanup_failure_is_not_suppressed(tmp_path: Path, monkeypatch):
    real_rmtree = shutil.rmtree
    created_area: Path | None = None

    def fail_cleanup(path, ignore_errors=False, **kwargs):
        if ignore_errors:
            return
        raise OSError("staging cleanup failed")

    monkeypatch.setattr("app.services.staging.shutil.rmtree", fail_cleanup)
    with pytest.raises(OSError, match="staging cleanup failed"):
        async with temporary_staging_area(
            tmp_path, transfer_id=uuid.uuid4(), expected_size=None
        ) as area:
            created_area = area
            (area / "partial.bin").write_bytes(b"partial")

    assert created_area is not None and created_area.exists()
    monkeypatch.setattr("app.services.staging.shutil.rmtree", real_rmtree)
    real_rmtree(created_area)


def test_orphan_staging_cleanup_only_removes_old_inactive_transfer_areas(tmp_path: Path):
    active_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    young_id = uuid.uuid4()
    active = tmp_path / f"transfer-{active_id.hex}-active"
    orphan = tmp_path / f"transfer-{orphan_id.hex}-orphan"
    young = tmp_path / f"transfer-{young_id.hex}-young"
    megacmd_home = tmp_path / f"megacmd-{uuid.uuid4().hex[:12]}-home"
    malformed = tmp_path / "transfer-not-a-uuid-or-file"
    for path in (active, orphan, young, megacmd_home, malformed):
        path.mkdir()

    old_time = time.time() - 3600
    os.utime(orphan, (old_time, old_time))
    external = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    external.mkdir()
    symlinked = tmp_path / f"transfer-{uuid.uuid4().hex}-symlink"
    symlinked.symlink_to(external, target_is_directory=True)

    removed = cleanup_orphaned_transfer_staging_areas(
        tmp_path,
        active_transfer_ids={active_id},
        minimum_age_seconds=600,
    )

    assert removed == 1
    assert active.is_dir()
    assert not orphan.exists()
    assert young.is_dir()
    assert megacmd_home.is_dir()
    assert malformed.is_dir()
    assert symlinked.is_symlink()
    assert external.is_dir()
