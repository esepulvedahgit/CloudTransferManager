from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

MegaCmdAction = Literal["logout", "ls", "get", "put", "mkdir", "df", "quit", "version"]
LineCallback = Callable[[str], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]


class MegaCmdError(RuntimeError):
    """A sanitized MEGAcmd execution failure."""

    @property
    def retryable(self) -> bool:
        return False


class MegaCmdCommandError(MegaCmdError):
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

    def __init__(self, return_code: int, output: bytes = b"") -> None:
        self.return_code = return_code
        message = output.decode("utf-8", errors="ignore").casefold()
        self._retryable = not any(marker in message for marker in self._fatal_markers) and any(
            marker in message for marker in self._retryable_markers
        )
        super().__init__(f"El comando MEGAcmd terminó con código {return_code}")

    @property
    def retryable(self) -> bool:
        return self._retryable


class MegaCmdCancelledError(MegaCmdError):
    pass


class MegaCmdTimeoutError(MegaCmdError):
    @property
    def retryable(self) -> bool:
        return True


class MegaCmdOutputLimitError(MegaCmdError):
    pass


class MegaCmdRunner:
    """Runs a small, explicit set of MEGAcmd clients without invoking a shell."""

    _allowed_actions = frozenset({"logout", "ls", "get", "put", "mkdir", "df", "quit", "version"})
    _max_output_bytes = 64 * 1024
    _max_log_line_bytes = 64 * 1024
    _terminate_grace_seconds = 1.0
    _poll_interval_seconds = 0.1
    _ls_time_format = "--time-format=ISO6081_WITH_TIME"

    def __init__(self, binary_dir: str) -> None:
        if not binary_dir or any(character in binary_dir for character in "\x00\r\n"):
            raise ValueError("Directorio de binarios MEGAcmd inválido")
        self.binary_dir = Path(binary_dir)

    async def run(
        self,
        action: MegaCmdAction,
        arguments: Sequence[str],
        *,
        home: Path,
        line_callback: LineCallback | None = None,
        should_cancel: CancelCheck | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, bytes]:
        argv = self._build_command(action, arguments)
        return await self._execute(
            argv,
            home=home,
            line_callback=line_callback,
            should_cancel=should_cancel,
            timeout_seconds=timeout_seconds,
        )

    async def login(
        self,
        username: str,
        password: str,
        *,
        home: Path,
        should_cancel: CancelCheck | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, bytes]:
        self._validate_token(username)
        self._validate_token(password)
        command = f"login {self._quote_token(username)} {self._quote_token(password)}\n".encode()
        try:
            stdout, stderr = await self._execute(
                [str(self.binary_dir / "mega-cmd")],
                home=home,
                stdin_data=command,
                should_cancel=should_cancel,
                timeout_seconds=timeout_seconds,
            )
        except MegaCmdError:
            raise
        except Exception:
            raise MegaCmdError("No fue posible iniciar sesión en MEGA") from None
        return self._redact((stdout, stderr), (username, password))

    def _build_command(self, action: MegaCmdAction, arguments: Sequence[str]) -> list[str]:
        if not isinstance(action, str) or action not in self._allowed_actions:
            raise ValueError("MEGAcmd action is not allowed")
        if isinstance(arguments, (str, bytes)):
            raise ValueError("MEGAcmd arguments must be a sequence of strings")
        values = list(arguments)
        for value in values:
            self._validate_token(value)

        if action == "ls":
            legacy_listing = (
                len(values) == 3 and values[:2] == ["-l", self._ls_time_format]
            )
            handle_listing = (
                len(values) == 4
                and values[:3] == ["-l", "--show-handles", self._ls_time_format]
            )
            if not legacy_listing and not handle_listing:
                raise ValueError("Invalid MEGAcmd arguments for ls")
            if not re.fullmatch(r"H:[A-Za-z0-9_-]{8}", values[-1]):
                values[-1] = self._normalize_remote_path(values[-1])
        elif action == "get":
            if len(values) != 2:
                raise ValueError("Invalid MEGAcmd arguments for get")
            if not re.fullmatch(r"H:[A-Za-z0-9_-]{8}", values[0]):
                values[0] = self._normalize_remote_path(values[0])
            values[1] = self._absolute_local_path(values[1])
        elif action == "put":
            if len(values) != 3 or values[0] != "-c":
                raise ValueError("Invalid MEGAcmd arguments for put")
            values[1] = self._absolute_local_path(values[1])
            values[2] = self._normalize_remote_path(values[2])
        elif action == "mkdir":
            if len(values) != 2 or values[0] != "-p":
                raise ValueError("Invalid MEGAcmd arguments for mkdir")
            values[1] = self._normalize_remote_path(values[1])
        elif action == "df":
            if values not in ([], ["-h"]):
                raise ValueError("Invalid MEGAcmd arguments for df")
        elif values:
            raise ValueError(f"Invalid MEGAcmd arguments for {action}")

        return [str(self.binary_dir / f"mega-{action}"), *values]

    async def _execute(
        self,
        argv: list[str],
        *,
        home: Path,
        line_callback: LineCallback | None = None,
        should_cancel: CancelCheck | None = None,
        timeout_seconds: float | None = None,
        stdin_data: bytes | None = None,
    ) -> tuple[bytes, bytes]:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        workspace = self._prepare_home(home)
        environment = os.environ.copy()
        environment["HOME"] = str(workspace)
        environment["TMPDIR"] = str(workspace)
        process: asyncio.subprocess.Process | None = None
        reader_tasks: list[asyncio.Task[tuple[bytes, bool]]] = []
        wait_task: asyncio.Task[int] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            if process.stdout is None or process.stderr is None:
                raise MegaCmdError("No se pudieron crear las tuberías del proceso MEGAcmd")

            async def read_stream(
                stream: asyncio.StreamReader,
                callback: LineCallback | None,
            ) -> tuple[bytes, bool]:
                captured = bytearray()
                pending = bytearray()
                discarding_long_line = False
                truncated = False
                while chunk := await stream.read(64 * 1024):
                    remaining = self._max_output_bytes - len(captured)
                    if remaining > 0:
                        captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                    if callback is None:
                        continue

                    while chunk:
                        if discarding_long_line:
                            newline = chunk.find(b"\n")
                            if newline < 0:
                                chunk = b""
                                continue
                            chunk = chunk[newline + 1 :]
                            discarding_long_line = False
                        newline = chunk.find(b"\n")
                        if newline < 0:
                            pending.extend(chunk)
                            if len(pending) > self._max_log_line_bytes:
                                pending.clear()
                                discarding_long_line = True
                            chunk = b""
                            continue
                        pending.extend(chunk[:newline])
                        if len(pending) <= self._max_log_line_bytes:
                            await callback(pending.decode("utf-8", errors="replace").rstrip("\r\n"))
                        pending.clear()
                        chunk = chunk[newline + 1 :]
                if pending and callback is not None and not discarding_long_line:
                    await callback(pending.decode("utf-8", errors="replace").rstrip("\r\n"))
                return bytes(captured), truncated

            reader_tasks = [
                asyncio.create_task(read_stream(process.stdout, line_callback)),
                asyncio.create_task(read_stream(process.stderr, line_callback)),
            ]
            if stdin_data is not None:
                if process.stdin is None:
                    raise MegaCmdError("No se pudo abrir la entrada de MEGAcmd")
                process.stdin.write(stdin_data)
                await process.stdin.drain()
                process.stdin.close()

            wait_task = asyncio.create_task(process.wait())
            started_at = time.monotonic()
            while not (wait_task.done() and all(task.done() for task in reader_tasks)):
                for task in reader_tasks:
                    if task.done() and not task.cancelled():
                        reader_error = task.exception()
                        if reader_error is not None:
                            raise reader_error
                if should_cancel is not None and await should_cancel():
                    await self._terminate(process, wait_task)
                    raise MegaCmdCancelledError("La operación MEGAcmd fue cancelada")
                if timeout_seconds is not None:
                    remaining_time = timeout_seconds - (time.monotonic() - started_at)
                    if remaining_time <= 0:
                        await self._terminate(process, wait_task)
                        raise MegaCmdTimeoutError("El comando MEGAcmd excedió el tiempo límite")
                    interval = min(self._poll_interval_seconds, remaining_time)
                else:
                    interval = self._poll_interval_seconds
                pending = {task for task in [wait_task, *reader_tasks] if not task.done()}
                if pending:
                    await asyncio.wait(pending, timeout=interval)

            stdout_result, stderr_result = await asyncio.gather(*reader_tasks)
            if process.returncode != 0:
                raise MegaCmdCommandError(
                    process.returncode or 0, stdout_result[0] + stderr_result[0]
                )
            if Path(argv[0]).name == "mega-ls" and (stdout_result[1] or stderr_result[1]):
                raise MegaCmdOutputLimitError("La salida MEGAcmd excedió el límite configurado")
            return stdout_result[0], stderr_result[0]
        except (MegaCmdError, asyncio.CancelledError):
            raise
        except (BrokenPipeError, OSError):
            raise MegaCmdError("No fue posible iniciar o completar el comando MEGAcmd") from None
        finally:
            if process is not None and process.returncode is None:
                if wait_task is None:
                    wait_task = asyncio.create_task(process.wait())
                await self._terminate(process, wait_task)
            if reader_tasks:
                for task in reader_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            if process is not None:
                self._close_transport(process)
            if wait_task is not None:
                if not wait_task.done():
                    wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)

    async def _terminate(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
    ) -> None:
        if process.returncode is not None:
            # A daemon can inherit the client's pipes after the client has exited.
            # Process.wait() still waits for EOF in that case; do not await it here.
            self._close_transport(process)
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=self._terminate_grace_seconds)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            self._close_transport(process)
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task), timeout=self._terminate_grace_seconds
                )
            except TimeoutError:
                raise MegaCmdError("No fue posible detener el cliente MEGAcmd") from None

    @staticmethod
    def _close_transport(process: asyncio.subprocess.Process) -> None:
        # asyncio exposes no public pipe-close API on Process. Closing its transport
        # releases inherited descriptors without killing the separate MEGAcmd daemon.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()

    @staticmethod
    def _prepare_home(home: Path) -> Path:
        workspace = Path(os.path.abspath(home))
        MegaCmdRunner._reject_symlink_components(workspace)
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        MegaCmdRunner._reject_symlink_components(workspace)
        os.chmod(workspace, 0o700)
        return workspace

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current = current / component
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(
                    "El directorio aislado MEGAcmd no puede contener un enlace simbólico"
                )
            if not stat.S_ISDIR(mode):
                raise ValueError("El directorio aislado MEGAcmd debe ser una carpeta")

    @staticmethod
    def _validate_token(value: str) -> None:
        if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
            raise ValueError("MEGAcmd argument contains an invalid character")
        if len(value.encode("utf-8")) > 16 * 1024:
            raise ValueError("MEGAcmd argument is too long")

    @staticmethod
    def _normalize_remote_path(value: str) -> str:
        if not value.startswith("/") or "\\" in value or "\x00" in value:
            raise ValueError("MEGAcmd paths must be absolute normalized paths")
        if any(part == ".." for part in value.split("/")):
            raise ValueError("Path traversal is not allowed")
        normalized = str(PurePosixPath(value))
        return "/" if normalized == "." else normalized

    @staticmethod
    def _absolute_local_path(value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("MEGAcmd local paths must be absolute")
        return str(path)

    @staticmethod
    def _quote_token(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _redact(output: tuple[bytes, bytes], secrets: Sequence[str]) -> tuple[bytes, bytes]:
        redacted: list[bytes] = []
        for chunk in output:
            for secret in sorted((value for value in secrets if value), key=len, reverse=True):
                chunk = chunk.replace(secret.encode("utf-8"), b"[REDACTED]")
            redacted.append(chunk)
        return redacted[0], redacted[1]
