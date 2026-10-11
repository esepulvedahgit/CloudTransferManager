import json
import uuid

import pytest

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


