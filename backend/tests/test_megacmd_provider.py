import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from app.providers.megacmd import MegaCmdProvider, MegaCmdSession
from app.services.megacmd import MegaCmdError

LISTING = (
    "d--- - - 2026-10-10T12:30:00 directorio con espacios\n"
    "---- 1 42 2026-10-09T08:15:00 archivo ñ.txt\n"
)


class ListingRunner:
    def __init__(self, listing: bytes = LISTING.encode(), quota: bytes = b"") -> None:
        self.listing = listing
        self.quota = quota
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, action, arguments, **kwargs):
        self.calls.append((action, list(arguments)))
        if action == "ls":
            return self.listing, b""
        if action == "df":
            return self.quota, b""
        return b"", b""

    async def login(self, username, password, **kwargs):
        return b"", b""


def make_provider(tmp_path: Path, runner: ListingRunner | None = None) -> MegaCmdProvider:
    return MegaCmdProvider(
        uuid.uuid4(),
        {"username": "mega@example.test", "password": "secret"},
        runner or ListingRunner(),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_connection_finishes_and_removes_home_when_logout_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class HangingLogoutRunner(ListingRunner):
        async def run(self, action, arguments, **kwargs):
            if action == "logout":
                self.calls.append((action, list(arguments)))
                await asyncio.sleep(2)
            return await super().run(action, arguments, **kwargs)

    monkeypatch.setattr(MegaCmdSession, "_cleanup_timeout_seconds", 0.05, raising=False)
    runner = HangingLogoutRunner()
    result = await asyncio.wait_for(make_provider(tmp_path, runner).test_connection(), 0.5)
    assert result.success
    assert [action for action, _ in runner.calls] == ["ls", "logout", "quit"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_lists_directories_and_files_preserving_spaces_and_unicode(tmp_path: Path):
    provider = make_provider(tmp_path)

    page = await provider.list_items("/")

    assert [(item.name, item.is_dir, item.size_bytes) for item in page.items] == [
        ("directorio con espacios", True, None),
        ("archivo ñ.txt", False, 42),
    ]
    assert page.items[0].path == "/directorio con espacios"
    assert page.items[1].path == "/archivo ñ.txt"


@pytest.mark.asyncio
async def test_listing_keeps_mega_handles_separate_from_duplicate_names(tmp_path: Path):
    listing = (
        b"---- 1 42 2026-10-09T08:15:00 History API.mp4 H:AbCd1234\n"
        b"---- 1 43 2026-10-09T08:15:00 History API.mp4 H:EfGh5678\n"
    )
    page = await make_provider(tmp_path, ListingRunner(listing)).list_items("/")

    assert [(item.name, item.object_id) for item in page.items] == [
        ("History API.mp4", "H:AbCd1234"),
        ("History API.mp4", "H:EfGh5678"),
    ]


@pytest.mark.asyncio
async def test_listing_handles_empty_and_malformed_rows(tmp_path: Path):
    provider = make_provider(tmp_path, ListingRunner(b"\nmalformed row\n"))

    page = await provider.list_items("/")

    assert page.items == []
    assert page.total_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [b"", b"---- 1 42 2026-10-09T08:15:00 NAME\n"])
async def test_listing_ignores_column_headers_but_keeps_real_file_named_NAME(
    tmp_path: Path, rows: bytes
):
    header = b"FLAGS VERS SIZE         DATE       NAME\n"
    provider = make_provider(tmp_path, ListingRunner(header + rows + header))

    page = await provider.list_items("/", page_size=1)

    assert [(item.name, item.path, item.size_bytes) for item in page.items] == (
        [("NAME", "/NAME", 42)] if rows else []
    )
    assert page.total_count == (1 if rows else 0)
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_listing_ignores_absolute_directory_titles_with_spaces(tmp_path: Path):
    listing = (
        b"/carpeta con espacios/subdirectorio de prueba:\n"
        b"FLAGS VERS SIZE DATE NAME\n"
        b"---- 1 42 2026-10-09T08:15:00 NAME\n"
    )
    page = await make_provider(tmp_path, ListingRunner(listing)).list_items("/", page_size=1)

    assert [(item.name, item.path) for item in page.items] == [("NAME", "/NAME")]
    assert page.total_count == 1
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_listing_supports_search_sort_and_pagination(tmp_path: Path):
    provider = make_provider(tmp_path)

    page = await provider.list_items("/", page_size=1, search="ARCHIVO")

    assert [item.name for item in page.items] == ["archivo ñ.txt"]
    assert page.next_cursor is None
    assert page.total_count == 1


@pytest.mark.asyncio
async def test_quota_parses_human_readable_mega_df_output(tmp_path: Path):
    runner = ListingRunner(quota=b"USED STORAGE: 5.00 GB 25.00% of 20.00 GB\n")
    provider = make_provider(tmp_path, runner)

    quota = await provider.get_quota()

    assert quota is not None
    assert quota.total_bytes == 20_000_000_000
    assert quota.used_bytes == 5_000_000_000
    assert quota.free_bytes == 15_000_000_000


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"username": "mega@example.test"},
        {"username": "", "password": "secret"},
        {"username": "mega@example.test", "password": "bad\ncommand"},
    ],
)
def test_rejects_invalid_credentials_without_network_login(credentials, tmp_path: Path):
    with pytest.raises(ValueError):
        MegaCmdProvider(uuid.uuid4(), credentials, ListingRunner(), tmp_path)


@pytest.mark.asyncio
async def test_test_connection_returns_sanitized_failure(tmp_path: Path):
    class FailedRunner(ListingRunner):
        async def login(self, username, password, **kwargs):
            raise RuntimeError(f"login failed for {username}: {password}")

    provider = make_provider(tmp_path, FailedRunner())

    result = await provider.test_connection()

    assert result.success is False
    assert result.message == "No fue posible verificar la conexión"
    assert "mega@example.test" not in result.message
    assert "secret" not in result.message


@pytest.mark.asyncio
async def test_session_reuses_login_and_cleans_private_home_after_file_operations(tmp_path: Path):
    class SessionRunner(ListingRunner):
        def __init__(self):
            super().__init__()
            self.login_count = 0
            self.homes: list[Path] = []

        async def login(self, username, password, *, home, **kwargs):
            self.login_count += 1
            self.homes.append(home)

        async def run(self, action, arguments, *, home, **kwargs):
            self.calls.append((action, list(arguments)))
            if action == "get":
                destination = Path(arguments[1]) / "archivo ñ.txt"
                destination.write_bytes(b"download")
            return b"", b""

    runner = SessionRunner()
    provider = make_provider(tmp_path, runner)
    local_file = tmp_path / "upload.txt"
    local_file.write_text("upload")

    async with provider.open_session(transfer_id=uuid.uuid4()) as session:
        downloaded = await session.download_file("/archivo ñ.txt", tmp_path / "download")
        await session.upload_file(local_file, "/nueva/carpeta")
        private_home = session.home
        assert private_home is not None
        assert private_home.stat().st_mode & 0o777 == 0o700

    assert downloaded.read_bytes() == b"download"
    assert runner.login_count == 1
    assert not private_home.exists()
    assert runner.calls == [
        ("get", ["/archivo ñ.txt", str((tmp_path / "download").resolve())]),
        ("put", ["-c", str(local_file.resolve()), "/nueva/carpeta"]),
        ("logout", []),
        ("quit", []),
    ]


@pytest.mark.asyncio
async def test_session_preserves_home_and_surfaces_failure_to_stop_daemon(tmp_path: Path):
    class QuitFailRunner(ListingRunner):
        fail_quit = True

        async def run(self, action, arguments, *, home, **kwargs):
            self.calls.append((action, list(arguments)))
            if action == "quit" and self.fail_quit:
                raise RuntimeError("daemon still running")
            return b"", b""

    runner = QuitFailRunner()
    provider = make_provider(tmp_path, runner)

    with pytest.raises(MegaCmdError, match="detener"):
        async with provider.open_session() as session:
            home = session.home

    assert home is not None and home.exists()
    runner.fail_quit = False
    await session.close()
    assert not home.exists()


@pytest.mark.asyncio
async def test_session_surfaces_private_home_removal_failure(tmp_path: Path, monkeypatch):
    real_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        raise OSError("home cleanup failed")

    provider = make_provider(tmp_path)
    monkeypatch.setattr("app.providers.megacmd.shutil.rmtree", fail_cleanup)

    with pytest.raises(MegaCmdError, match="retirar"):
        async with provider.open_session() as session:
            home = session.home

    assert home is not None and home.exists()
    monkeypatch.setattr("app.providers.megacmd.shutil.rmtree", real_rmtree)
    await session.close()
    assert not home.exists()


@pytest.mark.asyncio
async def test_failed_login_stops_daemon_before_removing_session_home(tmp_path: Path):
    class LoginFailRunner(ListingRunner):
        login_home: Path | None = None

        async def login(self, username, password, *, home, **kwargs):
            self.login_home = home
            raise RuntimeError("login failed")

    runner = LoginFailRunner()
    provider = make_provider(tmp_path, runner)

    with pytest.raises(RuntimeError, match="login failed"):
        async with provider.open_session():
            pytest.fail("login failure must prevent yielding a session")

    assert runner.calls == [("logout", []), ("quit", [])]
    assert runner.login_home is not None and not runner.login_home.exists()


@pytest.mark.asyncio
async def test_session_finishes_teardown_before_propagating_task_cancellation(tmp_path: Path):
    class SlowLogoutRunner(ListingRunner):
        def __init__(self):
            super().__init__()
            self.logout_started = asyncio.Event()
            self.allow_logout = asyncio.Event()

        async def run(self, action, arguments, *, home, **kwargs):
            self.calls.append((action, list(arguments)))
            if action == "logout":
                self.logout_started.set()
                await self.allow_logout.wait()
            return b"", b""

    runner = SlowLogoutRunner()
    provider = make_provider(tmp_path, runner)
    session_home: Path | None = None

    async def finish_session():
        nonlocal session_home
        async with provider.open_session() as session:
            session_home = session.home

    task = asyncio.create_task(finish_session())
    await runner.logout_started.wait()
    task.cancel()
    runner.allow_logout.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.calls == [("logout", []), ("quit", [])]
    assert session_home is not None and not session_home.exists()
