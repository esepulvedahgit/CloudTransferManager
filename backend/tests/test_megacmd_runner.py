import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.services.megacmd import MegaCmdCommandError, MegaCmdOutputLimitError, MegaCmdTimeoutError
from app.settings import Settings


def _runner_type() -> Any:
    if importlib.util.find_spec("app.services.megacmd") is None:
        pytest.fail("MEGAcmd runner behavior is not implemented")
    module = importlib.import_module("app.services.megacmd")
    runner_type = getattr(module, "MegaCmdRunner", None)
    if runner_type is None:
        pytest.fail("MegaCmdRunner is not implemented")
    return runner_type


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


def _ls_arguments(path: str = "/") -> list[str]:
    return ["-l", "--time-format=ISO6081_WITH_TIME", path]


@pytest.mark.asyncio
async def test_timeout_does_not_wait_for_descendant_inherited_pipes(tmp_path: Path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _executable(
        binary_dir / "mega-ls",
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])\n",
    )
    runner = _runner_type()(str(binary_dir))
    with pytest.raises(MegaCmdTimeoutError):
        await asyncio.wait_for(
            runner.run("ls", _ls_arguments(), home=tmp_path / "home", timeout_seconds=0.1),
            timeout=0.8,
        )


class _FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", *, returncode: int = 0):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode: int | None = returncode
        self._finished = asyncio.Event()
        if returncode is not None:
            self._finished.set()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self._finished.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._finished.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._finished.set()


@pytest.mark.asyncio
async def test_runner_passes_paths_as_argv_and_uses_isolated_home_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _executable(
        binary_dir / "mega-ls",
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'home': os.environ.get('HOME'), "
        "'tmpdir': os.environ.get('TMPDIR')}))\n",
    )
    home = tmp_path / "isolated home"
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_exec = asyncio.create_subprocess_exec

    async def capture_exec(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_exec)
    runner = runner_type(str(binary_dir))
    remote_path = "/folder/with spaces;$(not-a-shell-command)"

    stdout, stderr = await runner.run(
        "ls",
        ["-l", "--time-format=ISO6081_WITH_TIME", remote_path],
        home=home,
    )

    assert stderr == b""
    result = json.loads(stdout)
    assert result["argv"] == ["-l", "--time-format=ISO6081_WITH_TIME", remote_path]
    assert result["home"] == str(home)
    assert result["tmpdir"] == str(home)
    assert calls[0][0][0] == str(binary_dir / "mega-ls")
    assert "shell" not in calls[0][1]
    assert "stdin" not in calls[0][1] or calls[0][1]["stdin"] is None


@pytest.mark.asyncio
async def test_runner_rejects_unapproved_actions_and_control_characters(tmp_path: Path):
    runner = _runner_type()(str(tmp_path))

    with pytest.raises(ValueError, match="not allowed"):
        await runner.run("rm", ["/important"], home=tmp_path / "home")
    with pytest.raises(ValueError, match="invalid"):
        await runner.run("ls", _ls_arguments("/file\nlogout"), home=tmp_path / "home")
    with pytest.raises(ValueError, match="arguments"):
        await runner.run("put", ["/tmp/file"], home=tmp_path / "home")


@pytest.mark.asyncio
async def test_runner_rejects_symlink_components_in_isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(real_parent, target_is_directory=True)

    async def should_not_start(*args: Any, **kwargs: Any):
        pytest.fail("MEGAcmd must not start with a symlinked isolated HOME")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", should_not_start)
    runner = runner_type("/usr/bin")

    with pytest.raises(ValueError, match="enlace simbólico"):
        await runner.run("ls", _ls_arguments(), home=link / "session")

    assert not (real_parent / "session").exists()


@pytest.mark.asyncio
async def test_runner_rejects_truncated_directory_listing(tmp_path: Path):
    runner_type = _runner_type()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _executable(
        binary_dir / "mega-ls",
        "import sys\n"
        "sys.stdout.write('---- 1 4 2026-10-10T12:30:00 filename.txt\\n' * 3000)\n",
    )

    with pytest.raises(MegaCmdOutputLimitError, match="límite"):
        await runner_type(str(binary_dir)).run(
            "ls", _ls_arguments(), home=tmp_path / "session-home"
        )


def test_megacmd_command_errors_classify_transient_provider_failures():
    transient = MegaCmdCommandError(7, b"HTTP 503: service temporarily unavailable")
    authentication = MegaCmdCommandError(7, b"Authentication failed: invalid password")

    assert transient.retryable is True
    assert authentication.retryable is False
    assert "temporarily unavailable" not in str(transient)


@pytest.mark.asyncio
async def test_runner_classifies_transient_failure_without_exposing_provider_output(
    tmp_path: Path,
):
    runner_type = _runner_type()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _executable(
        binary_dir / "mega-ls",
        "import sys\n"
        "sys.stderr.write('HTTP 503: temporary failure for private-file-name\\n')\n"
        "sys.exit(7)\n",
    )

    with pytest.raises(MegaCmdCommandError) as error:
        await runner_type(str(binary_dir)).run(
            "ls", _ls_arguments(), home=tmp_path / "session-home"
        )

    assert error.value.retryable is True
    assert "private-file-name" not in str(error.value)


def test_settings_expose_megacmd_binary_and_disk_backed_staging_paths():
    settings = Settings(megacmd_binary_dir="/opt/mega/bin", transfer_staging_path="/data/staging")

    assert settings.megacmd_binary_dir == "/opt/mega/bin"
    assert settings.transfer_staging_path == "/data/staging"


@pytest.mark.asyncio
async def test_runner_terminates_process_when_transfer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    process = _FakeProcess(returncode=None)

    async def start(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    async def cancel_requested() -> bool:
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runner = runner_type("/usr/bin")

    with pytest.raises(RuntimeError, match="cancel"):
        await runner.run(
            "ls", _ls_arguments(), home=tmp_path / "home", should_cancel=cancel_requested
        )

    assert process.terminated


@pytest.mark.asyncio
async def test_runner_terminates_process_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    process = _FakeProcess(returncode=None)

    async def start(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runner = runner_type("/usr/bin")

    with pytest.raises(RuntimeError, match="tiempo límite"):
        await runner.run("ls", _ls_arguments(), home=tmp_path / "home", timeout_seconds=0.01)

    assert process.terminated


@pytest.mark.asyncio
async def test_runner_bounds_captured_output_and_sanitizes_provider_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    noisy = _FakeProcess(stdout=b"x" * 512_000, stderr=b"y" * 512_000)
    failed = _FakeProcess(stderr=b"authentication failed: provider-secret", returncode=7)
    processes = [noisy, failed]

    async def start(*args: Any, **kwargs: Any) -> _FakeProcess:
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runner = runner_type("/usr/bin")
    stdout, stderr = await runner.run("df", [], home=tmp_path / "home")

    assert len(stdout) < 128_000
    assert len(stderr) < 128_000
    with pytest.raises(RuntimeError) as error:
        await runner.run("ls", _ls_arguments(), home=tmp_path / "home")
    assert "provider-secret" not in str(error.value)
    assert "authentication failed" not in str(error.value).lower()


@pytest.mark.asyncio
async def test_login_sends_credentials_only_over_stdin_and_redacts_echoed_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    runner_type = _runner_type()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _executable(
        binary_dir / "mega-cmd",
        "import sys\n"
        "sys.stdout.write(sys.stdin.read())\n",
    )
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_exec = asyncio.create_subprocess_exec

    async def capture_exec(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_exec)
    username = "mega-user@example.test"
    password = "test-only-secret-9482"
    runner = runner_type(str(binary_dir))

    stdout, stderr = await runner.login(username, password, home=tmp_path / "session-home")

    assert calls[0][0] == (str(binary_dir / "mega-cmd"),)
    assert calls[0][1].get("stdin") == asyncio.subprocess.PIPE
    assert calls[0][1].get("shell") is None
    assert username.encode() not in stdout + stderr
    assert password.encode() not in stdout + stderr
    assert stdout == b'login "[REDACTED]" "[REDACTED]"\n'
