import json
import uuid

import pytest

from app.providers.nextcloud import NextcloudRcloneProvider
from app.services.rclone import RcloneCommandError, RcloneRunner
from app.settings import Settings


def _provider(runner=None, *, url="https://cloud.example.test/remote.php/dav/files/alice/"):
    return NextcloudRcloneProvider(
        uuid.uuid4(),
        {"url": url, "username": "alice", "password": "application password"},
        runner or RcloneRunner("/usr/bin/rclone", "/tmp"),
    )


def test_nextcloud_config_uses_webdav_vendor_and_obscures_app_password():
    provider = _provider()

    config = provider.config_content()

    assert "type = webdav" in config
    assert "vendor = nextcloud" in config
    assert "nextcloud_chunk_size = 10Mi" in config
    assert "url = https://cloud.example.test/remote.php/dav/files/alice/" in config
    assert "user = alice" in config
    assert "pass = application password" not in config


def test_nextcloud_builds_webdav_endpoint_from_base_url_and_username():
    provider = NextcloudRcloneProvider(
        uuid.uuid4(),
        {
            "url": "https://cloud.example.test/nextcloud/",
            "username": "alice@example.test",
            "password": "application password",
        },
        RcloneRunner("/usr/bin/rclone", "/tmp"),
    )

    assert (
        "url = https://cloud.example.test/nextcloud/remote.php/dav/files/"
        "alice%40example.test/"
    ) in provider.config_content()


def test_nextcloud_accepts_a_server_origin_entered_by_the_admin():
    provider = _provider(
        url="https://storage.example.test/remote.php/dav/files/alice/"
    )

    assert "url = https://storage.example.test/remote.php/dav/files/alice/" in (
        provider.config_content()
    )


def test_nextcloud_config_sets_a_bounded_webdav_upload_chunk_size():
    provider = NextcloudRcloneProvider(
        uuid.uuid4(),
        {
            "url": "https://cloud.example.test/remote.php/dav/files/alice/",
            "username": "alice",
            "password": "application password",
        },
        RcloneRunner("/usr/bin/rclone", "/tmp"),
        chunk_size="128Mi",
    )

    assert "nextcloud_chunk_size = 128Mi" in provider.config_content()


def test_nextcloud_rejects_unbounded_or_invalid_chunk_sizes():
    for chunk_size in ("0", "2Gi", "invalid", "1e6"):
        with pytest.raises(ValueError):
            NextcloudRcloneProvider(
                uuid.uuid4(),
                {
                    "url": "https://cloud.example.test/remote.php/dav/files/alice/",
                    "username": "alice",
                    "password": "application password",
                },
                RcloneRunner("/usr/bin/rclone", "/tmp"),
                chunk_size=chunk_size,
            )


def test_settings_expose_nextcloud_chunk_size_with_a_safe_default():
    assert Settings().nextcloud_chunk_size == "10Mi"
    assert Settings(nextcloud_chunk_size="128Mi").nextcloud_chunk_size == "128Mi"
    with pytest.raises(ValueError):
        Settings(nextcloud_chunk_size="2Gi")


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.example.test/remote.php/dav/files/alice/",
        "https://cloud.example.test/remote.php/webdav/",
        "https://cloud.example.test/../remote.php/dav/files/alice/",
        "https://alice:secret@cloud.example.test/remote.php/dav/files/alice/",
        "https://cloud.example.test/remote.php/dav/files/alice/?token=secret",
    ],
)
def test_nextcloud_rejects_invalid_server_urls(url):
    with pytest.raises(ValueError):
        _provider(url=url).config_content()


def test_nextcloud_accepts_percent_encoded_username_in_dav_path():
    credentials = NextcloudRcloneProvider.validate_credentials(
        {
            "url": "https://cloud.example.test/remote.php/dav/files/alice%40example.test/",
            "username": "alice@example.test",
            "password": "application password",
        }
    )

    assert credentials["username"] == "alice@example.test"


@pytest.mark.asyncio
async def test_nextcloud_lists_cache_the_remote_directory_across_pages():
    class ListingRunner:
        def __init__(self):
            self.calls = 0

        async def run(self, command, remote_paths, config, job_id, **kwargs):
            self.calls += 1
            assert command == "lsjson"
            assert remote_paths[0].endswith(":/")
            return (
                json.dumps(
                    [
                        {"Name": f"file-{index:03}.txt", "IsDir": False, "Size": index}
                        for index in range(60)
                    ]
                ).encode(),
                b"",
            )

    runner = ListingRunner()
    provider = _provider(runner)

    first = await provider.list_items("/", page_size=50)
    second = await provider.list_items("/", cursor=first.next_cursor, page_size=50)

    assert runner.calls == 1
    assert len(first.items) == 50
    assert len(second.items) == 10
    assert second.items[0].name == "file-050.txt"


@pytest.mark.asyncio
async def test_nextcloud_connection_test_bypasses_the_directory_cache():
    class ListingRunner:
        calls = 0

        async def run(self, command, remote_paths, config, job_id, **kwargs):
            self.calls += 1
            return b"[]", b""

    runner = ListingRunner()
    provider = _provider(runner)

    await provider.list_items("/")
    result = await provider.test_connection()

    assert result.success is True
    assert runner.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", [b"directory not found", b"HTTP 404 Not Found"])
async def test_nextcloud_stat_returns_none_for_missing_destination(stderr):
    class Runner:
        async def run(self, *args, **kwargs):
            raise RcloneCommandError(3, stderr)

    assert await _provider(Runner()).get_item("/new-folder/file.docx") is None


@pytest.mark.asyncio
async def test_nextcloud_stat_does_not_hide_permission_errors():
    class Runner:
        async def run(self, *args, **kwargs):
            raise RcloneCommandError(1, b"HTTP 403 Forbidden")

    with pytest.raises(RcloneCommandError):
        await _provider(Runner()).get_item("/new-folder/file.docx")


@pytest.mark.asyncio
async def test_nextcloud_get_item_uses_rclone_stat_instead_of_listing_the_parent():
    class StatRunner:
        options = None

        async def run(self, command, remote_paths, config, job_id, **kwargs):
            self.options = kwargs
            assert command == "lsjson"
            assert remote_paths[0].endswith(":/folder/file.txt")
            return (
                json.dumps(
                    {"Name": "file.txt", "Path": "folder/file.txt", "Size": 12, "IsDir": False}
                ).encode(),
                b"",
            )

    runner = StatRunner()
    provider = _provider(runner)

    item = await provider.get_item("/folder/file.txt")

    assert item is not None
    assert item.path == "/folder/file.txt"
    assert item.size_bytes == 12
    assert isinstance(runner.options, dict)
    assert runner.options["timeout_seconds"] == 30
