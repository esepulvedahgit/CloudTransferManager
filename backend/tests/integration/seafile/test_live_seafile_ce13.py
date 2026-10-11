from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.providers.seafile import SeafileRcloneProvider
from app.providers.seafile_api import SeafileApiProvider
from app.providers.seafile_auth import SeafileAuthClient, normalize_seafile_base_url
from app.services.rclone import RcloneRunner

_CLEANUP_CONFIRMATION = "DELETE_CTM_COMPATIBILITY_PREFIX_ONLY"
_ROOT_PATTERN = re.compile(r"^/ctm-compat-[0-9a-f]{32}$")


def _remote_file_path(root: str, directory: str, source_file_name: str) -> str:
    return f"{root.rstrip('/')}/{directory.strip('/')}/{source_file_name}"


def test_live_rclone_path_uses_uploaded_source_filename() -> None:
    assert _remote_file_path("/compat", "rclone", "origen Rclone.bin") == (
        "/compat/rclone/origen Rclone.bin"
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"SEAFILE_INTEGRATION=1 requires {name}")
    return value


@pytest.mark.asyncio
async def test_live_seafile_ce_13_api_and_rclone_round_trip(tmp_path: Path) -> None:
    """Exercise real reads/writes only after explicit CE 13 and cleanup opt-in."""
    if os.environ.get("SEAFILE_INTEGRATION") != "1":
        pytest.skip("Set SEAFILE_INTEGRATION=1 to run the live Seafile test")

    base_url = normalize_seafile_base_url(_required_environment("SEAFILE_TEST_URL"))
    username = _required_environment("SEAFILE_TEST_USERNAME")
    password = _required_environment("SEAFILE_TEST_PASSWORD")
    library_name = _required_environment("SEAFILE_TEST_LIBRARY")
    if os.environ.get("SEAFILE_TEST_CONFIRM_CLEANUP") != _CLEANUP_CONFIRMATION:
        pytest.fail(
            "Set SEAFILE_TEST_CONFIRM_CLEANUP=DELETE_CTM_COMPATIBILITY_PREFIX_ONLY "
            "to authorize cleanup of this test's UUID-scoped directory"
        )

    otp = os.environ.get("SEAFILE_TEST_OTP") or None
    credentials: dict[str, Any] = {
        "url": base_url,
        "username": username,
        "password": password,
        "library": library_name,
        "auth_token": await SeafileAuthClient(base_url).obtain_token(username, password, otp),
    }
    library_password = os.environ.get("SEAFILE_TEST_LIBRARY_PASSWORD")
    if library_password:
        credentials["library_password"] = library_password

    token = credentials["auth_token"]
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        server_info_response = await client.get(f"{base_url}/api2/server-info/", headers=headers)
        assert server_info_response.status_code == 200, "Could not read Seafile server info"
        server_info = server_info_response.json()
        version = server_info.get("version") if isinstance(server_info, dict) else None
        features = server_info.get("features", []) if isinstance(server_info, dict) else []
        assert isinstance(version, str) and version.startswith("13."), (
            "This opt-in harness is scoped to Seafile 13.x; "
            f"server reported {version!r}"
        )
        assert isinstance(features, list) and "seafile-pro" not in features, (
            "This opt-in harness requires Community Edition; server reports seafile-pro"
        )

        libraries_response = await client.get(f"{base_url}/api2/repos/", headers=headers)
        assert libraries_response.status_code == 200, "Could not list Seafile libraries"
        libraries = libraries_response.json()
        assert isinstance(libraries, list), "Seafile library list response was invalid"
        library = next(
            (
                value
                for value in libraries
                if isinstance(value, dict) and value.get("name") == library_name
            ),
            None,
        )
        assert isinstance(library, dict) and isinstance(library.get("id"), str), (
            "The configured disposable test library was not found"
        )
        library_id = library["id"]

    api = SeafileApiProvider(uuid.uuid4(), credentials, timeout_seconds=120, chunk_size=1024 * 1024)
    connection_result = await api.test_connection()
    assert connection_result.success, "Seafile API connection test failed"
    root = f"/ctm-compat-{uuid.uuid4().hex}"
    assert _ROOT_PATTERN.fullmatch(root), "Generated cleanup path escaped its exact prefix"
    cleanup_needed = False
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    config_dir = tmp_path / "rclone-config"
    rclone_binary = os.environ.get("RCLONE_BINARY", "/usr/bin/rclone")
    assert Path(rclone_binary).is_file() or shutil.which(rclone_binary), (
        "Rclone binary is required for the live compatibility test"
    )
    runner = RcloneRunner(rclone_binary, str(config_dir), str(staging))
    rclone = SeafileRcloneProvider(uuid.uuid4(), credentials, runner)

    api_source = staging / "origen API.bin"
    api_source.write_bytes(bytes(range(256)) * 9217)
    api_digest = hashlib.sha256(api_source.read_bytes()).hexdigest()
    api_remote_path = f"{root}/api/café archivo.bin"
    rclone_source = staging / "origen Rclone.bin"
    rclone_source.write_bytes(bytes(reversed(range(256))) * 8193)
    rclone_digest = hashlib.sha256(rclone_source.read_bytes()).hexdigest()
    rclone_remote_path = _remote_file_path(root, "rclone", rclone_source.name)
    empty_source = staging / "vacío 0 bytes.txt"
    empty_source.touch()

    try:
        cleanup_needed = True
        await api.mkdir(root)
        rclone_connection_result = await rclone.test_connection()
        assert rclone_connection_result.success, "Rclone Seafile connection test failed"
        await api.upload_file(api_source, api_remote_path)
        await api.upload_file(empty_source, f"{root}/api/vacío 0 bytes.txt")
        assert await api.sha256(api_remote_path) == api_digest
        empty_remote_path = f"{root}/api/vacío 0 bytes.txt"
        assert await api.sha256(empty_remote_path) == hashlib.sha256(b"").hexdigest()
        api_download = tmp_path / "descarga-api" / api_source.name
        await api.download_file(api_remote_path, api_download)
        assert hashlib.sha256(api_download.read_bytes()).hexdigest() == api_digest
        empty_download = tmp_path / "descarga-api" / empty_source.name
        await api.download_file(empty_remote_path, empty_download)
        assert empty_download.read_bytes() == b""

        await api.mkdir(f"{root}/rclone")
        await runner.copy_local_to_remote(
            rclone_source,
            rclone.remote_path(f"{root}/rclone"),
            rclone.config_content(),
            uuid.uuid4(),
        )
        rclone_listing = await rclone.list_items(f"{root}/rclone")
        assert any(item.name == rclone_source.name for item in rclone_listing.items)
        assert await api.sha256(rclone_remote_path) == rclone_digest
        rclone_download_directory = staging / "descarga-rclone"
        rclone_download_directory.mkdir(mode=0o700)
        await runner.copy_remote_to_local(
            rclone.remote_path(rclone_remote_path),
            rclone_download_directory,
            rclone.config_content(),
            uuid.uuid4(),
        )
        rclone_download = rclone_download_directory / rclone_source.name
        assert hashlib.sha256(rclone_download.read_bytes()).hexdigest() == rclone_digest
        assert list(config_dir.iterdir()) == [], "Rclone temporary config was not removed"
    finally:
        if cleanup_needed:
            assert _ROOT_PATTERN.fullmatch(root), "Refusing to clean outside the test prefix"
            item = await api.get_item(root)
            if item is not None:
                async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                    cleanup_response = await client.delete(
                        f"{base_url}/api2/repos/{library_id}/dir/",
                        params={"p": root},
                        headers=headers,
                    )
                assert cleanup_response.status_code == 200, (
                    "Could not remove the test-owned Seafile directory"
                )
