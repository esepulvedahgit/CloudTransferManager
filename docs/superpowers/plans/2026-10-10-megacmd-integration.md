# MEGAcmd Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Work inline in this session; the user already requested implementation in-session.

**Goal:** Replace the unsupported Rclone MEGA backend with official MEGAcmd for MEGA operations, preserve Rclone for Google Drive, and stage at most one file per transfer at a time with immediate cleanup.

**Architecture:** Add a strict asynchronous MEGAcmd subprocess runner and isolated per-operation sessions; expose MEGA through the existing `StorageProvider` interface. Login credentials go to `mega-cmd` through `stdin`, never process arguments. For cross-provider copies, enumerate files and stage each individually in a configurable disk-backed worker directory, using Rclone only on the Google Drive side and deleting each staging area in `finally`.

**Tech Stack:** Python 3.12, FastAPI, asyncio subprocesses, MEGAcmd official Debian package, existing Rclone CLI, Docker Compose, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-10-megacmd-integration-design.md`

## Global Constraints

- Transfers remain `COPY` only; never delete or alter source files.
- Never invoke MEGAcmd or Rclone with a shell; pass an argument list to subprocess execution.
- Rclone's strict command allowlist remains `lsjson`, `copy`, `about`, `size`; forbidden operations stay forbidden.
- PostgreSQL, Redis, workers, and transfer engines do not publish host ports; only the reverse proxy is exposed.
- Provider credentials remain AES-256-GCM encrypted at rest, are never returned to the frontend, and must not appear in logs.
- Rclone configuration remains per-job, mode `0600`, and is removed in `finally`.
- MEGAcmd session/cache and each per-file staging area are isolated and cleaned on success, failure, or cancellation. If `mega-quit` cannot be confirmed, retain the HOME and fail safely instead of deleting files under a potentially live daemon.
- MEGA credentials must not appear in subprocess argv, captured error text, progress output, or logs; login uses `mega-cmd` stdin because `mega-login` takes the password as an argv parameter.
- Do not stage, commit, or overwrite the user's pre-existing work. The current checkout contains user changes and untracked project files; keep changes scoped to the paths below.

## Review Focus

- Remote/local names with spaces, Unicode, leading dashes, or path traversal must remain data, not command options or shell syntax; Task 2 and Task 3 tests cover these.
- MEGAcmd listing output with names containing spaces, empty output, or unsupported entry types must parse safely or fail with a sanitized provider error; truncated listings must fail closed; Task 2 tests cover these.
- A file larger than currently available staging space must fail before download and leave no partial file; Task 3 tests cover this.
- Cancellation or an exception during either side of a copy must terminate the subprocess and clean the file/session workspace; Tasks 1, 3, and 4 cover these.
- Repeated/overlapping source paths and a pre-existing MEGA destination item must not create an extra remote copy; Task 4 tests cover these.
- Transient read/download and Drive-copy failures use bounded backoff and fresh staging. A failed MEGA upload is ambiguous, so it is never automatically retried; the destination is inspected and the transfer fails with a sanitized uncertainty diagnostic.

---

### Task 1: Safe MEGAcmd process runner and isolated session lifecycle

**Files:**
- Create: `backend/app/services/megacmd.py`
- Create: `backend/tests/test_megacmd_runner.py`
- Modify: `backend/app/settings.py`

**Interfaces:**
- Produce `MegaCmdAction = Literal["logout", "ls", "get", "put", "df", "quit", "version"]`.
- Produce `MegaCmdRunner(binary_dir: str)` with `async run(action: MegaCmdAction, arguments: Sequence[str], *, home: Path, line_callback: LineCallback | None = None, should_cancel: CancelCheck | None = None, timeout_seconds: float | None = None) -> tuple[bytes, bytes]` and `async login(username: str, password: str, *, home: Path, timeout_seconds: float | None = None) -> tuple[bytes, bytes]`.
- `run` resolves only `{binary_dir}/mega-{action}`. `login` resolves `{binary_dir}/mega-cmd`, starts it with no credential arguments, writes one safely encoded `login <username> <password>` command to stdin, and closes stdin so the shell exits without placing credentials in argv. Both paths set `HOME` and `TMPDIR` to the isolated workspace, use `asyncio.create_subprocess_exec`, bound captured output, and raise sanitized errors containing no credentials or raw provider output. Login output is redacted for username/password before it can be returned or logged; the session verifies login with an allowed root listing.
- Define `LineCallback = Callable[[str], Awaitable[None]]` and `CancelCheck = Callable[[], Awaitable[bool]]`. Argument validation rejects NUL/CR/LF and enforces command-specific argument shapes; remote paths are normalized before reaching MEGAcmd.
- Add settings `megacmd_binary_dir: str = "/usr/bin"` and `transfer_staging_path: str = "/var/lib/cloud-transfer/staging"` for later tasks; Task 5 supplies the container directory and package.

- [x] **Step 1: Write failing runner tests** for safe argv/no shell and isolated HOME, rejected commands, cancellation termination, timeout, bounded output, credential-free errors/output, and proving username/password only enter login via stdin (never argv).
- [x] **Step 2: Run RED** — `cd backend && pytest tests/test_megacmd_runner.py -v`; expected failures are missing runner behavior, not test/import errors.
- [x] **Step 3: Implement the allowlisted async runner** with process termination on cancellation/timeout and sanitized exception messages.
- [x] **Step 4: Run GREEN** — `cd backend && pytest tests/test_megacmd_runner.py -v`; all runner tests pass.

### Task 2: MEGAcmd provider and API connection wiring

**Files:**
- Create: `backend/app/providers/megacmd.py`
- Create: `backend/tests/test_megacmd_provider.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/providers/rclone.py`
- Modify: `backend/tests/test_rclone_providers.py`
- Modify: `backend/tests/test_connection_credentials.py`
- Modify: `backend/tests/test_health.py`

**Interfaces:**
- Produce `MegaCmdSession` as an async context manager that logs in once per isolated HOME and provides `list_items(path: str, cursor: str | None = None, page_size: int = 200) -> PaginatedItems`, `get_item(path: str) -> StorageItem | None`, `get_quota() -> StorageQuota | None`, `download_file(remote_path: str, destination_dir: Path, *, line_callback: LineCallback | None = None, should_cancel: CancelCheck | None = None) -> Path`, `upload_file(local_file: Path, remote_folder: str, *, line_callback: LineCallback | None = None, should_cancel: CancelCheck | None = None) -> None`, `close()`, and `__aexit__` cleanup. `upload_file` uses only `mega-put -c` to create missing remote directories. `open_session(*, transfer_id: UUID | None = None, should_cancel: CancelCheck | None = None) -> AsyncContextManager[MegaCmdSession]` returns this context manager. `close()` logs out, confirms the isolated MEGAcmd server quit, then removes the session HOME; if quit fails, preserve the HOME and surface a sanitized error.
- Produce `MegaCmdProvider(connection_id: UUID, credentials: dict[str, Any], runner: MegaCmdRunner, staging_path: Path)` implementing `StorageProvider`, plus `open_session(*, transfer_id: UUID | None = None, should_cancel: CancelCheck | None = None) -> AsyncContextManager[MegaCmdSession]` for transfer reuse.
- Parse `mega-ls -l --time-format=ISO6081_WITH_TIME` output into `StorageItem`; preserve the remainder of each row as the name so names with spaces survive. Parse `mega-df` into `StorageQuota`; unknown optional metadata remains `None`.
- `main.py` selects `MegaCmdProvider` for MEGA and `RcloneGoogleDriveProvider` for Google Drive. Connection create/update validates MEGA credential shape without a network login and stores credentials through the existing cipher. Production readiness checks for executable MEGAcmd and Rclone binaries.

- [x] **Step 1: Add failing provider tests** for a valid listing fixture (directory/file, spaces and Unicode in names), empty listing, malformed rows, sort/search/pagination, quota parsing, credential validation, test-connection error sanitization, and readiness when MEGAcmd is missing.
- [x] **Step 2: Run RED** — `cd backend && pytest tests/test_megacmd_provider.py tests/test_connection_credentials.py tests/test_health.py -v`; expected failures identify the absent provider/wiring.
- [x] **Step 3: Implement `MegaCmdSession` and `MegaCmdProvider`**, then wire API provider construction and remove the `RcloneMegaProvider` class/imports while preserving Google Drive's Rclone implementation.
- [x] **Step 4: Run GREEN** — `cd backend && pytest tests/test_megacmd_provider.py tests/test_connection_credentials.py tests/test_rclone_providers.py tests/test_health.py -v`; MEGA and Google provider tests pass, credentials remain absent from API responses, and readiness checks both binaries.

### Task 3: Confined local-copy support and per-file staging cleanup

**Files:**
- Create: `backend/app/services/staging.py`
- Create: `backend/tests/test_staging.py`
- Modify: `backend/app/services/rclone.py`
- Modify: `backend/tests/test_rclone_runner.py`

**Interfaces:**
- Produce `@asynccontextmanager temporary_staging_area(root: Path, *, transfer_id: UUID, expected_size: int | None) -> AsyncIterator[Path]`.
- The staging helper creates a unique private directory under the configured worker staging root, checks free space before yielding when size is known, and removes the complete area in `finally`.
- Add `RcloneRunner(binary: str, temp_path: str, staging_path: str | None = None)` and explicit operations `copy_remote_to_local(remote_source: str, local_directory: Path, config_content: str, job_id: UUID, *, line_callback: LineCallback | None = None, should_cancel: CancelCheck | None = None) -> None` and `copy_local_to_remote(local_file: Path, remote_destination: str, config_content: str, job_id: UUID, *, line_callback: LineCallback | None = None, should_cancel: CancelCheck | None = None) -> None`; local paths must contain no symlink components and resolve under `staging_path`. Preserve the existing remote-only Rclone path checks and command allowlist; both helpers invoke only `copy`.

- [x] **Step 1: Add failing staging and Rclone tests** for cleanup after normal exit, exception, and `CancelledError`; insufficient disk space before copy; local paths inside/outside the root; and traversal/symlink rejection.
- [x] **Step 2: Run RED** — `cd backend && pytest tests/test_staging.py tests/test_rclone_runner.py -v`; expected failures are missing staging/local-copy behavior.
- [x] **Step 3: Implement the staging context manager and confined Rclone copy variants** without enabling any additional Rclone subcommands.
- [x] **Step 4: Run GREEN** — `cd backend && pytest tests/test_staging.py tests/test_rclone_runner.py -v`; temporary directories disappear on every exit path and invalid local paths are rejected.

### Task 4: Per-file bidirectional transfer orchestration

**Files:**
- Modify: `backend/app/services/transfer_executor.py`
- Modify: `backend/app/services/telemetry.py`
- Modify: `backend/tests/test_transfer_executor.py`
- Modify: `backend/tests/test_telemetry.py`

**Interfaces:**
- `TransferExecutor` uses `MegaCmdProvider.open_session()` once per job and the Task 3 staging context once per file.
- Recursively enumerate leaf files through paginated provider/session listing; preserve relative paths for nested files; de-duplicate normalized source paths before transfer.
- MEGA → Drive calls `session.download_file(remote_path, staging_area, ...)` then `RcloneRunner.copy_local_to_remote(...)`; Drive → MEGA calls `RcloneRunner.copy_remote_to_local(...)` then `session.upload_file(local_file, remote_folder, ...)`.
- Before a Mega upload, resolve the exact destination path and call `session.get_item`; if it already exists, skip that item and increment warning metadata. Upload missing paths with `mega-put -c`; never overwrite/delete the remote item.
- Retry classified transient MEGAcmd/Rclone errors for safe read/download and Drive-copy steps with bounded backoff, recording attempt/error metadata. Do not retry after a failed MEGA upload because the server may have accepted a partial or complete file; fail with `MEGA_DESTINATION_STATE_UNCERTAIN` instead of risking duplicate remote names.
- Remove each per-file staging area before beginning the next file. Construct the transfer executor's Rclone runner with `settings.transfer_staging_path` so its local-copy confinement matches the staging context root.
- Progress callbacks update current item, bytes, speed, ETA, and SSE snapshots from Rclone JSON stats and supported MEGAcmd progress output. Unrecognized progress lines are ignored, not treated as transfer failure.

- [x] **Step 1: Add failing executor tests** for both directions, nested folders, overlapping selections, MEGA destination conflict warning, per-file cleanup before the next file, failure on either leg, and cancellation on either leg.
- [x] **Step 2: Run RED** — `cd backend && pytest tests/test_transfer_executor.py -v`; expected failures show the unsupported provider pair and missing cleanup/orchestration.
- [x] **Step 3: Implement the Mega/Drive execution branch** while leaving the existing mock vertical slice unchanged; add numeric-only MEGAcmd progress parsing with representative output fixtures.
- [x] **Step 4: Run GREEN** — `cd backend && pytest tests/test_transfer_executor.py tests/test_telemetry.py tests/test_transfer_stream.py -v`; both directions, cleanup guarantees, and event progress pass.

### Task 5: Container setup, readiness, and specification alignment

**Files:**
- Modify: `backend/Dockerfile`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `docs/specs/02-storage-providers-spec.md`
- Modify: `docs/specs/04-transfers-engine-spec.md`
- Modify: `docs/specs/05-realtime-telemetry-spec.md`
- Modify: `docs/specs/07-infrastructure-docker-spec.md`
- Modify: `docs/adr/ADR-001-rclone-subprocess-security.md`
- Modify: `scripts/check_compose_network.py`

**Interfaces:**
- Pin the Python image to Debian 12 Bookworm, add MEGA's signed Debian 12 package repository, install the official MEGAcmd package, and verify `/usr/bin/mega-version` and `/usr/bin/mega-cmd`; retain Rclone for Drive.
- Create/chown the configurable staging directory for `ctm`; do not mount it as a persistent Docker volume. Keep the small Rclone config tmpfs separate.
- Task 2 owns production readiness checks for the required MEGAcmd and Rclone binaries; this task verifies the installed image provides them.
- Update formal specs/ADR to distinguish MEGAcmd (MEGA) from Rclone (Google Drive), document per-file cleanup and collision skip behavior, and retain private-network and COPY-only invariants.

- [x] **Step 1: Add a failing Compose assertion** that staging is not a persistent volume or published port.
- [x] **Step 2: Run RED** — `python scripts/check_compose_network.py`; expected failure identifies missing staging network/volume assertion.
- [x] **Step 3: Implement the image/Compose/settings documentation changes** and ensure the official binary version command works in the image.
- [x] **Step 4: Run verification** — `cd backend && pytest -v --tb=short`, `cd backend && ruff check .`, `cd backend && mypy app`, `docker compose config`, and `docker compose build backend worker`; expect exit code 0 for each. Do not run `docker compose up` as part of this plan.

## References

- [MEGAcmd official README](https://github.com/meganz/MEGAcmd): server/client model and session/cache lifecycle.
- [MEGAcmd `ls` command](https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/ls.md): listing fields and ISO time format.
- [MEGAcmd `get` command](https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/get.md): file/folder download semantics; the implementation downloads leaf files individually.
- [MEGAcmd `put` command](https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/put.md): `-c` creates missing destination directories for uploads.
- [MEGAcmd User Guide](https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md): scriptable command usage and background server behavior.
- [MEGAcmd login command docs](https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/login.md): login syntax.
- [MEGAcmd issue #868](https://github.com/meganz/MEGAcmd/issues/868): maintainer-confirmed option of sending the login command to `mega-cmd` over stdin instead of exposing a password in argv.
