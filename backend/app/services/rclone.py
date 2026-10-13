import asyncio
import fcntl
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath


class RcloneCommandError(RuntimeError):
    _fatal_markers = (
        "authentication failed",
        "invalid password",
        "unauthorized",
        "permission denied",
        "not found",
        "no such file",
        "404",
        "401",
        "403",
    )
    _retryable_markers = (
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "temporary failure",
        "temporarily unavailable",
        "unexpected eof",
        "429",
        "500",
        "502",
        "503",
        "504",
        "rate limit",
        "network is unreachable",
    )

    def __init__(self, return_code: int, stderr: bytes = b"") -> None:
        self.return_code = return_code
        self._stderr = stderr
        super().__init__(f"Rclone terminó con código {return_code}")

    @property
    def retryable(self) -> bool:
        message = self._stderr.decode("utf-8", errors="ignore").casefold()
        if any(marker in message for marker in self._fatal_markers):
            return False
        return any(marker in message for marker in self._retryable_markers)


class RcloneCancelledError(RuntimeError):
    pass


class RcloneOutputLimitError(RuntimeError):
    pass


class RcloneRunner:
    _allowed_commands = {"lsjson", "copy", "about", "size"}
    _copy_output_limit = 64 * 1024
    _json_output_limit = 32 * 1024 * 1024
    _max_log_line_bytes = 64 * 1024
    _remote_path = re.compile(r"^(ctm_[0-9a-f]{32}):(/[^\x00\r\n]*|[^\x00\r\n]+)$")

    def __init__(self, binary: str, temp_path: str, staging_path: str | None = None) -> None:
        self.binary = binary
        self.temp_path = temp_path
        self.staging_path = Path(staging_path) if staging_path is not None else None

    @classmethod
    def _validate_remote_path(cls, value: str) -> str:
        match = cls._remote_path.fullmatch(value)
        if match is None:
            raise ValueError("Rclone paths must use an internal remote and a normalized path")
        path = match.group(2)
        if "\\" in path or any(part == ".." for part in path.split("/")):
            raise ValueError("Path traversal is not allowed")
        if path.startswith("/") and str(PurePosixPath(path)) != path.rstrip("/") and path != "/":
            raise ValueError("Rclone path is not normalized")
        if not path.startswith("/") and (
            any(part in {"", "."} for part in path.split("/")) or str(PurePosixPath(path)) != path
        ):
            raise ValueError("Rclone path is not normalized")
        return value

    def build_command(
        self,
        command: str,
        remote_paths: list[str],
        config_path: str,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
        *,
        create_empty_src_dirs: bool = False,
    ) -> list[str]:
        if command not in self._allowed_commands:
            raise ValueError(f"Rclone command is not allowed: {command}")
        if create_empty_src_dirs and command != "copy":
            raise ValueError("Empty source directories can only be preserved by copy")
        expected_paths = 2 if command == "copy" else 1
        if len(remote_paths) != expected_paths:
            raise ValueError(f"{command} expects {expected_paths} remote path(s)")
        paths = [self._validate_remote_path(path) for path in remote_paths]
        argv = [self.binary, command, *paths, "--config", config_path]
        if command == "copy":
            if conflict_policy not in {"SKIP", "OVERWRITE_IF_DIFFERENT"}:
                raise ValueError("Unsupported copy conflict policy")
            argv.extend(["--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"])
            if conflict_policy == "SKIP":
                argv.append("--ignore-existing")
            if create_empty_src_dirs:
                argv.append("--create-empty-src-dirs")
        elif command == "about":
            argv.extend(["--json", "--full"])
        elif command == "size":
            argv.append("--json")
        return argv

    async def run(
        self,
        command: str,
        remote_paths: list[str],
        config_content: str,
        job_id: uuid.UUID,
        line_callback: Callable[[str], Awaitable[None]] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
        create_empty_src_dirs: bool = False,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, bytes]:
        if command not in self._allowed_commands:
            raise ValueError(f"Rclone command is not allowed: {command}")
        if len(remote_paths) != (2 if command == "copy" else 1):
            raise ValueError(f"{command} expects {2 if command == 'copy' else 1} remote path(s)")
        for path in remote_paths:
            self._validate_remote_path(path)
        return await self._run_with_argv(
            command,
            config_content,
            job_id,
            lambda config_path: self.build_command(
                command,
                remote_paths,
                config_path,
                conflict_policy,
                create_empty_src_dirs=create_empty_src_dirs,
            ),
            line_callback=line_callback,
            should_cancel=should_cancel,
            timeout_seconds=timeout_seconds,
        )

    async def copy_remote_to_local(
        self,
        remote_source: str,
        local_directory: Path,
        config_content: str,
        job_id: uuid.UUID,
        *,
        line_callback: Callable[[str], Awaitable[None]] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
    ) -> None:
        safe_remote_source = self._validate_remote_path(remote_source)
        safe_local_directory = self._validate_local_staging_path(local_directory, is_directory=True)
        await self._run_with_argv(
            "copy",
            config_content,
            job_id,
            lambda config_path: self._copy_command(
                safe_remote_source, safe_local_directory, config_path, conflict_policy
            ),
            line_callback=line_callback,
            should_cancel=should_cancel,
        )

    async def copy_local_to_remote(
        self,
        local_file: Path,
        remote_destination: str,
        config_content: str,
        job_id: uuid.UUID,
        *,
        line_callback: Callable[[str], Awaitable[None]] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
    ) -> None:
        safe_local_file = self._validate_local_staging_path(local_file, is_directory=False)
        safe_remote_destination = self._validate_remote_path(remote_destination)
        await self._run_with_argv(
            "copy",
            config_content,
            job_id,
            lambda config_path: self._copy_command(
                safe_local_file, safe_remote_destination, config_path, conflict_policy
            ),
            line_callback=line_callback,
            should_cancel=should_cancel,
        )

    async def copy_local_directory_to_remote(
        self,
        local_directory: Path,
        remote_destination: str,
        config_content: str,
        job_id: uuid.UUID,
        *,
        line_callback: Callable[[str], Awaitable[None]] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
    ) -> None:
        safe_local_directory = self._validate_local_staging_path(local_directory, is_directory=True)
        safe_remote_destination = self._validate_remote_path(remote_destination)
        await self._run_with_argv(
            "copy",
            config_content,
            job_id,
            lambda config_path: self._copy_command(
                safe_local_directory,
                safe_remote_destination,
                config_path,
                conflict_policy,
                create_empty_src_dirs=True,
            ),
            line_callback=line_callback,
            should_cancel=should_cancel,
        )

    def _validate_local_staging_path(self, value: Path, *, is_directory: bool) -> str:
        if self.staging_path is None:
            raise ValueError("Rclone local copies require a configured staging path")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Rclone local paths must be absolute and inside staging")
        staging_root = self.staging_path.absolute()
        try:
            self._reject_symlinks(staging_root)
            self._reject_symlinks(path)
            resolved_root = staging_root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
            relative_path = resolved_path.relative_to(resolved_root)
            if not relative_path.parts:
                raise ValueError("Rclone local path must be a child of staging")
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "symlink" in str(exc).casefold():
                raise
            raise ValueError(
                "Rclone local path must resolve inside staging without symlinks"
            ) from None
        if is_directory:
            if not resolved_path.is_dir():
                raise ValueError("Rclone local destination must be a directory inside staging")
        elif not resolved_path.is_file():
            raise ValueError("Rclone local source must be a file inside staging")
        return str(resolved_path)

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current = current / component
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError("Rclone local paths cannot contain symlinks")

    def _copy_command(
        self,
        source: str,
        destination: str,
        config_path: str,
        conflict_policy: str = "OVERWRITE_IF_DIFFERENT",
        *,
        create_empty_src_dirs: bool = False,
    ) -> list[str]:
        if conflict_policy not in {"SKIP", "OVERWRITE_IF_DIFFERENT"}:
            raise ValueError("Unsupported copy conflict policy")
        command = [
            self.binary,
            "copy",
            source,
            destination,
            "--config",
            config_path,
            "--use-json-log",
            "--stats",
            "1s",
            "--stats-log-level",
            "NOTICE",
        ]
        if conflict_policy == "SKIP":
            command.append("--ignore-existing")
        if create_empty_src_dirs:
            command.append("--create-empty-src-dirs")
        return command

    async def _run_with_argv(
        self,
        command: str,
        config_content: str,
        job_id: uuid.UUID,
        argv_builder: Callable[[str], list[str]],
        *,
        line_callback: Callable[[str], Awaitable[None]] | None = None,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, bytes]:
        if "\x00" in config_content:
            raise ValueError("Rclone configuration contains an invalid character")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Rclone timeout must be positive")
        os.makedirs(self.temp_path, mode=0o700, exist_ok=True)
        os.chmod(self.temp_path, 0o700)
        config_path = os.path.join(self.temp_path, f"{job_id}.rclone.conf")
        descriptor = os.open(
            config_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        process: asyncio.subprocess.Process | None = None
        reader_tasks: list[asyncio.Task[tuple[bytes, bool]]] = []
        wait_task: asyncio.Task[int] | None = None
        owns_config_lock = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Rclone transfer is already running") from None
            owns_config_lock = True
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Rclone configuration path must be a regular file")
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as config_file:
                config_file.write(config_content)
                config_file.flush()
            argv = argv_builder(config_path)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Rclone process pipes were not created")

            async def read_stream(
                stream: asyncio.StreamReader,
                callback: Callable[[str], Awaitable[None]] | None = None,
            ) -> tuple[bytes, bool]:
                chunks: list[bytes] = []
                captured = 0
                truncated = False
                pending = bytearray()
                discarding_long_line = False
                output_limit = (
                    self._copy_output_limit if command == "copy" else self._json_output_limit
                )
                while chunk := await stream.read(64 * 1024):
                    remaining = output_limit - captured
                    if remaining > 0:
                        retained = chunk[:remaining]
                        chunks.append(retained)
                        captured += len(retained)
                    if len(chunk) > remaining:
                        truncated = True
                    if callback is None:
                        continue
                    pending.extend(chunk)
                    while True:
                        if discarding_long_line:
                            newline = pending.find(b"\n")
                            if newline < 0:
                                pending.clear()
                                break
                            del pending[: newline + 1]
                            discarding_long_line = False
                        newline = pending.find(b"\n")
                        if newline < 0:
                            if len(pending) > self._max_log_line_bytes:
                                pending.clear()
                                discarding_long_line = True
                            break
                        line = bytes(pending[:newline])
                        del pending[: newline + 1]
                        if len(line) <= self._max_log_line_bytes:
                            await callback(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                if pending and callback is not None and not discarding_long_line:
                    await callback(pending.decode("utf-8", errors="replace").rstrip("\r\n"))
                return b"".join(chunks), truncated

            reader_tasks = [
                asyncio.create_task(read_stream(process.stdout)),
                asyncio.create_task(read_stream(process.stderr, line_callback)),
            ]
            wait_task = asyncio.create_task(process.wait())
            deadline = (
                asyncio.get_running_loop().time() + timeout_seconds
                if timeout_seconds is not None
                else None
            )
            while not wait_task.done():
                if should_cancel is not None and await should_cancel():
                    process.terminate()
                    try:
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await wait_task
                    await asyncio.gather(*reader_tasks, return_exceptions=True)
                    raise RcloneCancelledError("La transferencia fue cancelada")
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("Rclone operation timed out")
                    wait_timeout = min(0.25, remaining)
                else:
                    wait_timeout = 0.25
                await asyncio.wait({wait_task}, timeout=wait_timeout)
            (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
                *reader_tasks
            )
            if process.returncode != 0:
                raise RcloneCommandError(process.returncode or 1, stderr)
            if command != "copy" and (stdout_truncated or stderr_truncated):
                raise RcloneOutputLimitError("Rclone output exceeded the configured limit")
            return stdout, stderr
        except BaseException:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            for task in reader_tasks:
                if not task.done():
                    task.cancel()
            if reader_tasks:
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            raise
        finally:
            if owns_config_lock and os.path.exists(config_path):
                os.unlink(config_path)
            os.close(descriptor)
