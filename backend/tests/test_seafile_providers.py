import hashlib
import re
import uuid
from pathlib import Path

import httpx
import pytest

from app.providers.seafile import SeafileRcloneProvider
from app.providers.seafile_api import (
    SeafileApiCancelledError,
    SeafileApiError,
    SeafileApiProvider,
)
from app.services.rclone import RcloneRunner

ACCOUNT = "account-token"
REPOSITORY_ID = "f64d0704-64f2-497b-8d3a-8eb2b19e408a"
BASE_URL = "https://seafile.example.test"


def test_rclone_seafile_config_uses_account_token_and_obscures_library_password():
    provider = SeafileRcloneProvider(
        uuid.uuid4(),
        {
            "url": BASE_URL,
            "username": "operator@example.test",
            "auth_token": ACCOUNT,
            "library": "Archive",
            "library_password": "library password",
        },
        RcloneRunner("/usr/bin/rclone", "/tmp"),
    )

    config = provider.config_content()

    assert "type = seafile" in config
    assert f"url = {BASE_URL}/" in config
    assert "user = operator@example.test" in config
    assert "auth_token = account-token" in config
    assert "library = Archive" in config
    key_line = next(line for line in config.splitlines() if line.startswith("library_key = "))
    key_value = key_line.removeprefix("library_key = ")
    assert key_value != "library password"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", key_value)


@pytest.mark.asyncio
async def test_api_lists_encrypted_selected_library_and_unlocks_only_when_required():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api2/auth/ping/":
            assert request.headers["Authorization"] == f"Token {ACCOUNT}"
            return httpx.Response(200, text="pong")
        if request.url.path == "/api2/repos/":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": REPOSITORY_ID,
                        "name": "Archive",
                        "encrypted": True,
                        "mtime": 1780000000,
                    }
                ],
            )
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/":
            return httpx.Response(200, json={"encrypted": True})
        if request.method == "POST" and request.url.path == f"/api2/repos/{REPOSITORY_ID}/":
            assert b"password=library+secret" in request.content
            return httpx.Response(200, text='"success"')
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/":
            assert request.url.params["p"] == "/"
            return httpx.Response(
                200,
                json=[
                    {"name": "resume résumé.txt", "type": "file", "size": 5, "mtime": 1780000000},
                    {"name": "Deep Folder", "type": "dir", "size": 0, "mtime": 1780000001},
                ],
            )
        return httpx.Response(404)

    provider = SeafileApiProvider(
        uuid.uuid4(),
        {
            "url": BASE_URL,
            "username": "operator@example.test",
            "password": "login-secret",
            "auth_token": ACCOUNT,
            "library": "Archive",
            "library_password": "library secret",
        },
        transport=httpx.MockTransport(handler),
    )

    result = await provider.test_connection()
    page = await provider.list_items("/", page_size=1)

    assert result.success is True
    assert page.total_count == 2
    assert page.next_cursor == "1"
    assert [item.name for item in page.items] == ["Deep Folder"]
    assert page.items[0].path == "/Deep Folder"
    assert any(request.method == "POST" for request in requests)


@pytest.mark.asyncio
async def test_api_lists_all_libraries_and_resolves_unicode_and_space_paths():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api2/repos/":
            return httpx.Response(
                200,
                json=[{"id": REPOSITORY_ID, "name": "資料 Archive", "encrypted": False}],
            )
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/":
            return httpx.Response(200, json={"encrypted": False})
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/":
            assert request.url.params["p"] == "/Deep Folder"
            return httpx.Response(
                200,
                json=[{"name": "données résumé.txt", "type": "file", "size": 4, "mtime": 0}],
            )
        return httpx.Response(404)

    provider = SeafileApiProvider(
        uuid.uuid4(),
        {"url": BASE_URL, "username": "user", "password": "password", "auth_token": ACCOUNT},
        transport=httpx.MockTransport(handler),
    )

    root = await provider.list_items("/")
    nested = await provider.list_items("/資料 Archive/Deep Folder")
    selected = await provider.get_item("/資料 Archive/Deep Folder/données résumé.txt")

    assert root.items[0].path == "/資料 Archive"
    assert nested.items[0].path == "/資料 Archive/Deep Folder/données résumé.txt"
    assert selected is not None and selected.size_bytes == 4
    assert all(request.headers["Authorization"] == f"Token {ACCOUNT}" for request in requests)


@pytest.mark.asyncio
async def test_api_streams_file_download_and_sha256_without_buffering_response():
    data = b"large streamed contents"
    progress: list[tuple[int, int | None]] = []

    async def on_progress(current: int, total: int | None) -> None:
        progress.append((current, total))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/file/":
            assert request.url.params["p"] == "/report.txt"
            return httpx.Response(200, json=f"{BASE_URL}/seafhttp/files/{'a' * 40}/report.txt")
        if request.url.path.startswith("/seafhttp/files/"):
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    provider = _provider(httpx.MockTransport(handler), library="Archive")
    destination = Path("/tmp/seafile-download-test.txt")
    try:
        await provider.download_file("/report.txt", destination, progress_callback=on_progress)
        assert destination.read_bytes() == data
        assert await provider.sha256("/report.txt") == hashlib.sha256(data).hexdigest()
        assert progress[-1][0] == len(data)
    finally:
        destination.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_api_resumable_upload_starts_at_existing_offset_and_uses_bounded_chunks(
    tmp_path: Path,
):
    upload_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/" and request.method == "POST":
            return httpx.Response(200, text='"success"')
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/upload-link/":
            assert request.url.params["p"] == "/nested"
            return httpx.Response(200, json=f"{BASE_URL}/upload-api/upload-token")
        if request.url.path == f"/api/v2.1/repos/{REPOSITORY_ID}/file-uploaded-bytes/":
            assert request.url.params["parent_dir"] == "/nested"
            assert request.url.params["file_name"] == "résumé file.bin"
            return httpx.Response(200, json={"uploadedBytes": 2})
        if request.url.path == "/upload-api/upload-token":
            upload_requests.append(request)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    provider = _provider(httpx.MockTransport(handler), library="Archive", chunk_size=64 * 1024)
    source = tmp_path / "résumé file.bin"
    source.write_bytes(b"a" * 131_074)
    progress: list[tuple[int, int | None]] = []

    async def on_progress(current: int, total: int | None) -> None:
        progress.append((current, total))

    await provider.upload_file(
        source,
        "/nested/résumé file.bin",
        replace=True,
        progress_callback=on_progress,
    )

    assert [request.headers["Content-Range"] for request in upload_requests] == [
        "bytes 2-65537/131074",
        "bytes 65538-131073/131074",
    ]
    assert b"a" * 64_000 in upload_requests[0].content
    disposition = upload_requests[0].headers["Content-Disposition"]
    assert "filename*=" in disposition and "r%C3%A9sum%C3%A9%20file.bin" in disposition
    assert all(
        request.headers["Authorization"] == f"Token {ACCOUNT}" for request in upload_requests
    )
    assert progress == [(65_538, 131_074), (131_074, 131_074)]


@pytest.mark.asyncio
async def test_api_upload_supports_zero_byte_file_without_invalid_content_range(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/":
            return httpx.Response(200, json=[])
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/upload-link/":
            return httpx.Response(200, json=f"{BASE_URL}/upload-api/upload-token")
        if request.url.path == f"/api/v2.1/repos/{REPOSITORY_ID}/file-uploaded-bytes/":
            return httpx.Response(200, json={"uploadedBytes": 0})
        if request.url.path == "/upload-api/upload-token":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    provider = _provider(httpx.MockTransport(handler), library="Archive")
    source = tmp_path / "empty.bin"
    source.write_bytes(b"")

    await provider.upload_file(source, "/empty.bin", replace=False)

    upload = next(request for request in requests if request.url.path == "/upload-api/upload-token")
    assert "Content-Range" not in upload.headers
    assert b"\r\n\r\n0\r\n" in upload.content


@pytest.mark.asyncio
async def test_api_provider_classifies_retryable_and_terminal_http_errors():
    provider = _provider(
        httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "3"})),
        library="Archive",
    )

    with pytest.raises(SeafileApiError) as error:
        await provider.list_items("/")

    assert error.value.retryable is True
    assert error.value.retry_after == 3
    assert "account-token" not in str(error.value)
    assert "Retry-After" not in str(error.value)

    terminal = _provider(
        httpx.MockTransport(lambda request: httpx.Response(403, text="private server detail")),
        library="Archive",
    )
    with pytest.raises(SeafileApiError) as forbidden:
        await terminal.list_items("/")
    assert forbidden.value.retryable is False
    assert "private server detail" not in str(forbidden.value)


@pytest.mark.asyncio
async def test_api_provider_rejects_cross_host_transfer_links():
    provider = _provider(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json="https://attacker.example.test/download?token=secret"
            )
        ),
        library="Archive",
    )

    with pytest.raises(SeafileApiError, match="transfer URL"):
        await provider.download_file("/report.txt", Path("/tmp/seafile-host-test"))


@pytest.mark.asyncio
async def test_api_provider_honors_cancellation_and_rejects_path_traversal(tmp_path: Path):
    provider = _provider(
        httpx.MockTransport(lambda request: httpx.Response(500)),
        library="Archive",
    )

    async def cancel() -> bool:
        return True

    with pytest.raises(SeafileApiCancelledError):
        await provider.download_file("/report.txt", tmp_path / "cancelled", should_cancel=cancel)
    with pytest.raises(ValueError, match="Unsafe Seafile path"):
        await provider.list_items("/../private")


@pytest.mark.asyncio
async def test_api_readback_hash_and_directory_creation_honor_cancellation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/file/":
            return httpx.Response(200, json=f"{BASE_URL}/seafhttp/files/report")
        if request.url.path == "/seafhttp/files/report":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024))
        return httpx.Response(200, json=[])

    provider = _provider(httpx.MockTransport(handler), library="Archive")

    async def cancel() -> bool:
        return True

    with pytest.raises(SeafileApiCancelledError):
        await provider.sha256("/report.txt", should_cancel=cancel)
    with pytest.raises(SeafileApiCancelledError):
        await provider.mkdir("/nested/child", should_cancel=cancel)


@pytest.mark.asyncio
async def test_api_upload_resumes_at_server_offset_and_rejects_offset_past_eof(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/dir/":
            return httpx.Response(200, json=[])
        if request.url.path == f"/api2/repos/{REPOSITORY_ID}/upload-link/":
            return httpx.Response(200, json=f"{BASE_URL}/upload-api/upload-token")
        if request.url.path == f"/api/v2.1/repos/{REPOSITORY_ID}/file-uploaded-bytes/":
            return httpx.Response(200, json={"uploadedBytes": 20})
        return httpx.Response(404)

    provider = _provider(httpx.MockTransport(handler), library="Archive")
    source = tmp_path / "short.txt"
    source.write_bytes(b"short")

    with pytest.raises(SeafileApiError, match="upload offset"):
        await provider.upload_file(source, "/short.txt", replace=True)


def _provider(
    transport: httpx.AsyncBaseTransport,
    *,
    library: str | None = None,
    chunk_size: int = 8 * 1024 * 1024,
) -> SeafileApiProvider:
    credentials = {
        "url": BASE_URL,
        "username": "operator@example.test",
        "password": "login-secret",
        "auth_token": ACCOUNT,
    }
    if library:
        credentials["library"] = library
    provider = SeafileApiProvider(
        uuid.uuid4(),
        credentials,
        transport=transport,
        chunk_size=chunk_size,
    )
    provider._libraries = [{"id": REPOSITORY_ID, "name": library or "Archive", "encrypted": False}]
    return provider
