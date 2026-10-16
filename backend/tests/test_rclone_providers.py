import json
import uuid
from pathlib import Path

import httpx
import pytest

from app.providers.base import StorageItem
from app.providers.rclone import RcloneGoogleDriveProvider
from app.services.rclone import RcloneRunner


def test_google_drive_config_contains_oauth_refresh_data_and_shared_drive():
    connection_id = uuid.uuid4()
    provider = RcloneGoogleDriveProvider(
        connection_id,
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "team_drive": "shared-drive-id",
        },
        RcloneRunner("/usr/bin/rclone", "/tmp"),
    )

    config = provider.config_content()
    token_line = next(line for line in config.splitlines() if line.startswith("token = "))
    token = json.loads(token_line.removeprefix("token = "))

    assert "type = drive" in config
    assert "client_id = client-id" in config
    assert token["access_token"] == "access-token"
    assert token["refresh_token"] == "refresh-token"
    assert "team_drive = shared-drive-id" in config


@pytest.mark.asyncio
async def test_google_export_negative_size_is_unknown():
    class Runner:
        async def run(self, *args, **kwargs):
            return b'[{"Name":"document.docx","IsDir":false,"Size":-1}]', b""

    provider = RcloneGoogleDriveProvider(uuid.uuid4(), {
        "client_id": "id", "client_secret": "secret", "refresh_token": "token"
    }, Runner())
    page = await provider.list_items("/")
    assert page.items[0].size_bytes is None


@pytest.mark.asyncio
async def test_modified_time_sort_handles_provider_items_without_timestamps():
    class ListingRunner:
        async def run(self, *args, **kwargs):
            return (
                json.dumps(
                    [
                        {"Name": "dated.txt", "IsDir": False, "ModTime": "2026-10-01T00:00:00Z"},
                        {"Name": "undated.txt", "IsDir": False},
                    ]
                ).encode(),
                b"",
            )

    provider = RcloneGoogleDriveProvider(
        uuid.uuid4(),
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
        },
        ListingRunner(),
    )

    page = await provider.list_items("/", sort_by="modified_at")

    assert [item.name for item in page.items] == ["undated.txt", "dated.txt"]


@pytest.mark.asyncio
async def test_get_item_finds_entry_after_first_200_items():
    class ListingRunner:
        async def run(self, command, paths, config, job_id, **kwargs):
            entries = [
                {"Name": f"file-{index:03}.txt", "IsDir": False, "Size": index}
                for index in range(205)
            ]
            return json.dumps(entries).encode(), b""

    provider = RcloneGoogleDriveProvider(
        uuid.uuid4(),
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
        },
        ListingRunner(),
    )

    item = await provider.get_item("/file-204.txt")

    assert item is not None
    assert item.path == "/file-204.txt"
    assert item.size_bytes == 204


@pytest.mark.asyncio
async def test_google_download_uses_stable_file_id_and_keeps_oauth_private(tmp_path: Path):
    if not hasattr(RcloneGoogleDriveProvider, "download_file_by_id"):
        pytest.fail("Google Drive ID-based downloads are not implemented")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={"access_token": "refreshed-access", "expires_in": 3600},
            )
        return httpx.Response(200, content=b"selected-file-bytes")

    provider = RcloneGoogleDriveProvider(
        uuid.uuid4(),
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
        },
        RcloneRunner("/usr/bin/rclone", "/tmp"),
        http_transport=httpx.MockTransport(handle),
    )
    item = StorageItem(
        name="duplicate.txt",
        path="/duplicate.txt",
        is_dir=False,
        size_bytes=19,
        provider_type="GOOGLE_DRIVE",
        object_id="drive-file-id-2",
    )

    await provider.download_file_by_id(item, tmp_path / "selected.txt")

    assert (tmp_path / "selected.txt").read_bytes() == b"selected-file-bytes"
    media_request = requests[-1]
    assert media_request.url.path.endswith("/files/drive-file-id-2")
    assert media_request.url.params.get("alt") == "media"
    assert media_request.headers["Authorization"] == "Bearer refreshed-access"
    assert "refresh-token" not in str(media_request.headers)


@pytest.mark.asyncio
async def test_google_get_item_rejects_an_id_outside_the_selected_parent():
    provider = RcloneGoogleDriveProvider(
        uuid.uuid4(),
        {"client_id": "id", "client_secret": "secret", "refresh_token": "token"},
        RcloneRunner("/usr/bin/rclone", "/tmp"),
    )

    async def metadata(_object_id, *, include_download_capability=False):
        return {
            "id": "file-id",
            "name": "document.txt",
            "mimeType": "text/plain",
            "parents": ["different-parent"],
        }

    provider._google_file_metadata = metadata

    item = await provider.get_item(
        "/folder/document.txt",
        object_id="file-id",
        parent_object_id="requested-parent",
    )

    assert item is None


