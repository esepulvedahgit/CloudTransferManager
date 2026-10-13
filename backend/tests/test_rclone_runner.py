import asyncio
import os
import uuid

import pytest

from app.services.rclone import (
    RcloneCancelledError,
    RcloneCommandError,
    RcloneExecutionProfile,
    RcloneRunner,
)


def test_nextcloud_profile_adds_bounded_resource_flags_to_all_rclone_commands():
    runner = RcloneRunner(
        binary="/usr/bin/rclone",
        temp_path="/tmp",
        transfer_profile=RcloneExecutionProfile(),
    )

    command = runner.build_command(
        "copy",
        [
            "ctm_0123456789abcdef0123456789abcdef:/source",
            "ctm_fedcba9876543210fedcba9876543210:/dest",
        ],
        "/tmp/job.conf",
    )

    assert command[-10:] == [
        "--transfers",
        "1",
        "--checkers",
        "2",
        "--bwlimit",
        "8M",
        "--contimeout",
        "10s",
        "--timeout",
        "60s",
    ]


def test_rclone_retries_nextcloud_webdav_locks():
    error = RcloneCommandError(1, b"HTTP 423 Locked")

    assert error.retryable is True


def test_rclone_command_builder_uses_argv_and_closed_allowlist():
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")

    command = runner.build_command(
        "copy",
        [
            "ctm_0123456789abcdef0123456789abcdef:/source",
            "ctm_fedcba9876543210fedcba9876543210:/dest",
        ],
        "/tmp/job.conf",
    )

    assert command == [
        "/usr/bin/rclone",
        "copy",
        "ctm_0123456789abcdef0123456789abcdef:/source",
        "ctm_fedcba9876543210fedcba9876543210:/dest",
        "--config",
        "/tmp/job.conf",
        "--use-json-log",
        "--stats",
        "1s",
        "--stats-log-level",
        "NOTICE",
    ]


def test_rclone_copy_command_applies_only_supported_conflict_policy():
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")
    command = runner.build_command(
        "copy",
        [
            "ctm_0123456789abcdef0123456789abcdef:/source",
            "ctm_fedcba9876543210fedcba9876543210:/destination",
        ],
        "/tmp/job.conf",
        conflict_policy="SKIP",
    )
    assert command[-1] == "--ignore-existing"
    with pytest.raises(ValueError):
        runner.build_command(
            "copy",
            [
                "ctm_0123456789abcdef0123456789abcdef:/source",
                "ctm_fedcba9876543210fedcba9876543210:/destination",
            ],
            "/tmp/job.conf",
            conflict_policy="../../shell",
        )


def test_rclone_copy_command_can_preserve_empty_selected_folder_structure():
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")

    command = runner.build_command(
        "copy",
        [
            "ctm_0123456789abcdef0123456789abcdef:/source",
            "ctm_fedcba9876543210fedcba9876543210:/destination/source",
        ],
        "/tmp/job.conf",
        create_empty_src_dirs=True,
    )

    assert command[-1] == "--create-empty-src-dirs"


@pytest.mark.parametrize("command", ["delete", "purge", "sync", "move", "serve", "config"])
def test_rclone_command_builder_rejects_unapproved_commands(command: str):
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")

    with pytest.raises(ValueError):
        runner.build_command(command, ["ctm_0123456789abcdef0123456789abcdef:/x"], "/tmp/job.conf")


def test_rclone_command_builder_rejects_traversal_paths():
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")

    with pytest.raises(ValueError):
        runner.build_command(
            "lsjson", ["ctm_0123456789abcdef0123456789abcdef:/../../etc"], "/tmp/job.conf"
        )


def test_rclone_error_retryability_is_classified_without_raw_message_exposure():
    transient = RcloneCommandError(1, b"HTTP 503: service temporarily unavailable")
    fatal = RcloneCommandError(1, b"Authentication failed: invalid password")
    assert transient.retryable is True
    assert fatal.retryable is False
    assert "password" not in str(fatal).lower()


@pytest.mark.parametrize(
    ("command", "options"),
    [("about", ["--json", "--full"]), ("size", ["--json"])],
)
def test_quota_and_size_commands_request_json(command: str, options: list[str]):
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path="/tmp")

    result = runner.build_command(
        command, ["ctm_0123456789abcdef0123456789abcdef:/"], "/tmp/job.conf"
    )

    assert result[-len(options) :] == options


@pytest.mark.asyncio
async def test_ephemeral_config_is_private_and_removed_if_process_creation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path=str(tmp_path))
    captured: dict[str, str | int] = {}

    async def fail_to_start(*args, **kwargs):
        config_path = args[args.index("--config") + 1]
        captured["config_path"] = config_path
        captured["mode"] = os.stat(config_path).st_mode & 0o777
        captured["shell"] = int("shell" in kwargs)
        raise OSError("binary unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_start)

    with pytest.raises(OSError):
        await runner.run(
            "lsjson",
            ["ctm_0123456789abcdef0123456789abcdef:/"],
            "[ctm_0123456789abcdef0123456789abcdef]\ntype = mega\n",
            uuid.UUID("01234567-89ab-cdef-0123-456789abcdef"),
        )

    assert captured["mode"] == 0o600
    assert captured["shell"] == 0
    assert not os.path.exists(str(captured["config_path"]))


@pytest.mark.asyncio
async def test_rclone_run_recovers_stale_config_after_worker_redelivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    job_id = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
    stale_path = tmp_path / f"{job_id}.rclone.conf"
    stale_path.write_text("stale provider credentials", encoding="utf-8")
    runner = RcloneRunner(binary="/usr/bin/rclone", temp_path=str(tmp_path))
    captured: dict[str, str] = {}

    async def fail_to_start(*args, **kwargs):
        config_path = args[args.index("--config") + 1]
        captured["content"] = open(config_path, encoding="utf-8").read()
        raise OSError("binary unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_start)

    with pytest.raises(OSError):
        await runner.run(
            "lsjson",
            ["ctm_0123456789abcdef0123456789abcdef:/"],
            "[ctm_0123456789abcdef0123456789abcdef]\ntype = mega\n",
            job_id,
        )

    assert captured["content"] == "[ctm_0123456789abcdef0123456789abcdef]\ntype = mega\n"
    assert not stale_path.exists()


@pytest.mark.asyncio
async def test_cancellation_terminates_subprocess_and_removes_ephemeral_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = None
            self._finished = asyncio.Event()
            self.terminated = False

        async def wait(self):
            await self._finished.wait()
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self._finished.set()

        def kill(self):
            self.returncode = -9
            self._finished.set()

    process = FakeProcess()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *args, **kwargs: _resolved(process)
    )

    async def cancel_requested():
        return True

    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path))
    with pytest.raises(RcloneCancelledError):
        await runner.run(
            "copy",
            [
                "ctm_0123456789abcdef0123456789abcdef:/source",
                "ctm_fedcba9876543210fedcba9876543210:/destination",
            ],
            "[mock]\ntype = mega\n",
            uuid.uuid4(),
            should_cancel=cancel_requested,
        )
    assert process.terminated
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_interactive_timeout_terminates_subprocess_and_removes_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    class SlowProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = None
            self._finished = asyncio.Event()
            self.terminated = False

        async def wait(self):
            await self._finished.wait()
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self._finished.set()

        def kill(self):
            self.returncode = -9
            self._finished.set()

    process = SlowProcess()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *args, **kwargs: _resolved(process)
    )
    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path))

    with pytest.raises(TimeoutError):
        await runner.run(
            "lsjson",
            ["ctm_0123456789abcdef0123456789abcdef:/"],
            "[mock]\ntype = smb\n",
            uuid.uuid4(),
            timeout_seconds=0.01,
        )

    assert process.terminated
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_copy_command_retains_only_bounded_process_output(monkeypatch, tmp_path):
    class NoisyProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            output = (b"x" * 1024 + b"\n") * 128
            self.stdout.feed_data(output)
            self.stdout.feed_eof()
            self.stderr.feed_data(output)
            self.stderr.feed_eof()
            self.returncode = 0

        async def wait(self):
            return 0

    process = NoisyProcess()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *args, **kwargs: _resolved(process)
    )
    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path))

    stdout, stderr = await runner.run(
        "copy",
        [
            "ctm_0123456789abcdef0123456789abcdef:/source",
            "ctm_fedcba9876543210fedcba9876543210:/destination",
        ],
        "[mock]\ntype = mega\n",
        uuid.uuid4(),
    )

    assert len(stdout) <= 64 * 1024
    assert len(stderr) <= 64 * 1024


@pytest.mark.asyncio
async def test_local_copy_helpers_use_only_copy_and_accept_paths_inside_staging(
    monkeypatch, tmp_path
):
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    local_directory = staging_root / "area with spaces"
    local_directory.mkdir()
    local_file = local_directory / "archivo ñ.txt"
    local_file.write_text("content")
    commands = []

    class FinishedProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = 0

        async def wait(self):
            return 0

    async def start(*args, **kwargs):
        commands.append((args, kwargs))
        return FinishedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path / "rclone-tmp"), str(staging_root))

    await runner.copy_remote_to_local(
        "ctm_0123456789abcdef0123456789abcdef:/origin/archivo ñ.txt",
        local_directory,
        "[source]\ntype = drive\n",
        uuid.uuid4(),
    )
    await runner.copy_local_to_remote(
        local_file,
        "ctm_fedcba9876543210fedcba9876543210:/target/archivo ñ.txt",
        "[destination]\ntype = drive\n",
        uuid.uuid4(),
        conflict_policy="SKIP",
    )

    assert [args[1] for args, _kwargs in commands] == ["copy", "copy"]
    assert commands[0][0][3] == str(local_directory.resolve())
    assert commands[1][0][2] == str(local_file.resolve())
    assert "--ignore-existing" in commands[1][0]
    assert commands[0][1].get("shell") is None
    assert list((tmp_path / "rclone-tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_local_directory_copy_creates_empty_folders_with_only_copy(monkeypatch, tmp_path):
    staging_root = tmp_path / "staging"
    source_directory = staging_root / "empty-folders" / "selected" / "nested"
    source_directory.mkdir(parents=True)
    commands = []

    class FinishedProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = 0

        async def wait(self):
            return 0

    async def start(*args, **kwargs):
        commands.append((args, kwargs))
        return FinishedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path / "rclone-tmp"), str(staging_root))

    await runner.copy_local_directory_to_remote(
        source_directory.parent.parent,
        "ctm_fedcba9876543210fedcba9876543210:/backup",
        "[destination]\ntype = drive\n",
        uuid.uuid4(),
    )

    argv = commands[0][0]
    assert argv[1] == "copy"
    assert argv[2] == str(source_directory.parent.parent.resolve())
    assert argv[3] == "ctm_fedcba9876543210fedcba9876543210:/backup"
    assert "--create-empty-src-dirs" in argv
    assert commands[0][1].get("shell") is None
    assert list((tmp_path / "rclone-tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_local_copy_helpers_reject_outside_and_symlink_paths(monkeypatch, tmp_path):
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = staging_root / "linked"
    symlink.symlink_to(outside, target_is_directory=True)
    runner = RcloneRunner("/usr/bin/rclone", str(tmp_path / "rclone-tmp"), str(staging_root))

    async def should_not_start(*args, **kwargs):
        pytest.fail("invalid local paths must be rejected before starting Rclone")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", should_not_start)
    with pytest.raises(ValueError, match="staging"):
        await runner.copy_remote_to_local(
            "ctm_0123456789abcdef0123456789abcdef:/file",
            outside,
            "[source]\ntype = drive\n",
            uuid.uuid4(),
        )

    with pytest.raises(ValueError, match="symlink"):
        await runner.copy_local_to_remote(
            symlink / "file.txt",
            "ctm_fedcba9876543210fedcba9876543210:/file.txt",
            "[source]\ntype = drive\n",
            uuid.uuid4(),
        )


async def _resolved(value):
    return value


def test_smb_remote_paths_use_a_share_relative_to_the_internal_remote():
    runner = RcloneRunner("rclone", "/tmp/rclone")

    command = runner.build_command(
        "lsjson",
        ["ctm_0123456789abcdef0123456789abcdef:Shared Data/Finance"],
        "/tmp/job.conf",
    )

    assert command[1:3] == [
        "lsjson",
        "ctm_0123456789abcdef0123456789abcdef:Shared Data/Finance",
    ]


def test_smb_remote_path_rejects_share_relative_traversal():
    runner = RcloneRunner("rclone", "/tmp/rclone")

    with pytest.raises(ValueError, match="traversal"):
        runner.build_command(
            "lsjson",
            ["ctm_0123456789abcdef0123456789abcdef:Share/../../etc"],
            "/tmp/job.conf",
        )
