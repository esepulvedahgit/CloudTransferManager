# Seafile Optional API Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Seafile CE 13 connections with rclone as the default adapter and an administrator-selected direct API adapter for browsing and COPY transfers.

**Architecture:** Persist `adapter_type` per connection, retain the existing encrypted credential envelope, and acquire the Seafile account token server-side with optional transient OTP. Use the rclone Seafile backend for its selected adapter and a focused HTTPX API provider for the explicitly selected API adapter; when a transfer uses the API, bridge it through private per-file staging and verify API destinations with SHA-256 readback.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy/Alembic, HTTPX, Rclone CLI, Celery worker, React/TypeScript, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-11-seafile-optional-api-design.md`

## Global Constraints

- Declare Seafile CE 13.x as the only supported Seafile branch initially.
- `adapter_type` is `RCLONE` by default or explicitly selected `SEAFILE_API`; never switch adapters after an error.
- Accept OTP only for authentication/testing; never persist OTP or a TOTP seed. Persist the resulting account token only in encrypted credentials.
- Keep provider credentials encrypted with the existing AES-256-GCM envelope and never return them to the frontend or logs.
- Invoke Rclone only as an argument list, without a shell, and only with `lsjson`, `copy`, `about`, or `size`.
- Transfers remain COPY-only; do not add remote delete, move, sync, or other destructive operations.
- Rclone config files remain per-operation temporary files with mode `0600` and guaranteed cleanup.
- API-involved transfers stage one file at a time in the existing private staging area and clean it on success, failure, and cancellation.
- Do not mark API-destination files complete until streamed readback SHA-256 matches the source file.

## Review Focus

- OTP or access token appearing in response, audit metadata, exception text, or logs — pin with API response/audit tests in Task 1 and sanitized provider-error tests in Task 2.
- API URL or Seafile-provided transfer URL crossing hosts — pin with URL validation and hostile-link tests in Task 2.
- Expired auth token silently switching adapters or being refreshed without OTP — pin with selected-adapter and reauthentication tests in Task 1.
- Interrupted or duplicate chunk upload corrupting a destination — pin resume-offset and final-hash tests in Task 2 and executor retry tests in Task 3.
- Unicode, slash-like path characters, traversal, empty files, and deeply nested paths — pin API path/mapping tests in Task 2 and transfer routing tests in Task 3.

---

### Task 1: Connection persistence, Seafile authentication, and adapter selection

**Files:**
- Create: `backend/app/providers/seafile_auth.py`
- Create: `backend/tests/test_seafile_auth.py`
- Create: `backend/migrations/versions/20261011_0003_seafile_adapter.py`
- Modify: `backend/app/models.py`
- Modify: `backend/app/services/memory_store.py`
- Modify: `backend/app/services/database_store.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_connection_credentials.py`
- Test: `backend/tests/test_database_store.py`
- Test: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: Existing `CredentialCipher`, `ConnectionRecord`, async store APIs, and `RcloneRunner`.
- Produces: `ConnectionRecord.adapter_type: str = "RCLONE"`; store creation accepts keyword `adapter_type`; `SeafileAuthClient(base_url, transport=None)` exposes `async obtain_token(username, password, otp=None) -> str` and `async ping(token) -> bool`; connection create/update accepts `adapter_type`; connection-test accepts an optional transient `{ "otp": "..." }` body.

- [x] **Step 1: Write failing tests** for migration defaults/check constraints, memory/database adapter persistence, Seafile CE API token login with and without OTP, sanitized login failure, public connection responses excluding credentials/OTP/token, and adapter/credential changes invalidating test status.
- [x] **Step 2: Run focused tests and confirm expected failures.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_auth.py tests/test_connection_credentials.py tests/test_database_store.py tests/test_migrations.py -q`. Expected: initial collection failure for the missing authenticator; after implementation, all pass.
- [x] **Step 3: Implement persistence and API contract.** Add the `SEAFILE` SQL check value, `adapter_type` column defaulting to `RCLONE`, migration, store mapping, input validation, audit-safe metadata, and server-side `POST {base}/api2/auth-token/` with `X-SEAFILE-OTP` only when supplied. The test endpoint obtains/renews a token when OTP is provided, stores it in the encrypted credential payload, and never stores OTP. Changes to adapter or credentials deactivate the connection until a successful test.
- [x] **Step 4: Run focused tests.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_auth.py tests/test_connection_credentials.py tests/test_database_store.py tests/test_migrations.py -q`. Expected: all pass; the migration metadata comparison has no differences and downgrade removes the new schema.
- [x] **Step 5: Commit** as `feat: add Seafile connection and adapter selection`.

### Task 2: Seafile rclone and direct API providers

**Files:**
- Create: `backend/app/providers/seafile.py`
- Create: `backend/app/providers/seafile_api.py`
- Create: `backend/tests/test_seafile_providers.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_rclone_providers.py`

**Interfaces:**
- Consumes: Task 1 `SeafileAuthClient`, encrypted credential payload with `url`, `username`, `password`, `auth_token`, optional `library` and `library_password`, and `adapter_type`.
- Produces: `SeafileRcloneProvider` extending `RcloneStorageProvider`, with a safe `[remote] type=seafile` config using `url`, `user`, `auth_token`, optional `library` and `library_key`; `SeafileApiProvider` implements `StorageProvider` plus `mkdir(path)`, `download_file(path, target, should_cancel)`, `upload_file(source, path, replace, should_cancel)`, and `sha256(path)`.

- [x] **Step 1: Write failing provider tests** for Rclone config/token/library options; HTTPX mock tests for auth ping, library listing, directory listing, item metadata, encrypted-library unlock, streamed download, resumable upload offset/chunk ranges, retryable versus terminal status, cancellation, URL host validation, and readback SHA-256. Include zero-byte files, Unicode/space names, and traversal rejection.
- [x] **Step 2: Run provider tests and confirm expected failures.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_providers.py tests/test_rclone_providers.py -q`. Expected: initial collection failure for the missing provider modules; after implementation, tests pass.
- [x] **Step 3: Implement both providers.** Use Seafile CE endpoints (`/api2/auth/ping/`, `/api2/repos/`, `/api2/repos/{repo_id}/`, `/api2/repos/{repo_id}/dir/`, `/api2/repos/{repo_id}/file/`, `/api2/repos/{repo_id}/upload-link/`, `/api/v2.1/repos/{repo_id}/file-uploaded-bytes/`, and the upload-link fileserver URL). Stream downloads and bounded chunks; resume from `uploadedBytes`; call the library decrypt API only for a selected encrypted library. Restrict transfer URLs to the configured server host and sanitize HTTP errors. Wire `provider_for` to the saved adapter without fallback.
- [x] **Step 4: Run provider tests.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_providers.py tests/test_rclone_providers.py -q`. Expected: pass without contacting an external Seafile server.
- [x] **Step 5: Commit** as `feat: add Seafile rclone and API providers`.

### Task 3: API transfer execution, integrity, cancellation, and retries

**Files:**
- Modify: `backend/app/services/transfer_executor.py`
- Modify: `backend/app/providers/seafile_api.py`
- Create: `backend/tests/test_seafile_transfers.py`
- Test: `backend/tests/test_transfer_executor.py`
- Test: `backend/tests/test_staging.py`

**Interfaces:**
- Consumes: Task 2 provider transfer methods and existing `temporary_staging_area`, `RcloneRunner`, `MegaCmdProvider`, and transfer progress/state helpers.
- Produces: Executor routing to the direct API only when a connection is saved as `SEAFILE_API`; direct Rclone-to-Rclone remains remote-to-remote; API-involved copies use one-file staging and API destinations are SHA-256 readback-verified before progress increments.

- [x] **Step 1: Write failing transfer tests** for API source to Rclone destination, Rclone source to API destination, API-to-API, no implicit fallback after API auth failure, destination checksum mismatch, API retry/resume, cancellation cleanup, nested/empty directory handling, and transfer completion only after verification.
- [x] **Step 2: Run tests and confirm failures.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_transfers.py -q`. Expected: API transfers are not currently routed.
- [x] **Step 3: Implement explicit route selection and staged copies.** Preserve existing Mega/Google behavior; use per-file helper methods for Seafile API and Rclone endpoints; carry cancellation into streaming and retry loops; do not add API delete/move/sync or silent adapter changes.
- [x] **Step 4: Run transfer tests.** Run: `cd backend && .venv/bin/pytest tests/test_seafile_transfers.py tests/test_transfer_executor.py tests/test_staging.py -q`. Expected: all pass and staging is removed after all terminal outcomes.
- [ ] **Step 5: Commit** as `feat: execute verified Seafile API transfers`.

### Task 4: Admin UI, API contract docs, compatibility harness, and full verification

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `docs/api/openapi-contracts.md`
- Modify: `docs/specs/02-storage-providers-spec.md`
- Modify: `docs/specs/04-transfers-engine-spec.md`
- Create: `docs/integrations/seafile-compatibility.md`
- Test: backend integration fixture/script under `backend/tests/integration/seafile/`

**Interfaces:**
- Consumes: Tasks 1–3 API fields, explicit adapter choice, and transient OTP test request.
- Produces: Admin can create a Seafile connection, select Rclone (default) or API, configure URL/account/library fields, and test with a transient OTP. Compatibility instructions document a destructive-safe disposable library and an opt-in CE 13.x integration run.

- [ ] **Step 1: Write failing UI tests** for Seafile form fields, default Rclone selection, explicit API selection, POST payload without secrets in responses, and transient OTP test behavior.
- [ ] **Step 2: Run UI tests and confirm failures.** Run: `cd frontend && npm test -- --run src/App.test.tsx`. Expected: no Seafile connection form exists yet.
- [ ] **Step 3: Implement UI and documentation.** Add admin-only Seafile setup and adapter selection; show adapter choice in connection inventory; add test/re-auth input for OTP without retaining it in form state after submission. Update public API and provider/transfer specs and provide an opt-in integration test against Seafile CE 13.x that creates, uploads, downloads, hashes, and removes only its own disposable test data after explicit environment configuration.
- [ ] **Step 4: Run focused and full verification.** Run: `cd frontend && npm test -- --run`; `cd frontend && npm run lint`; `cd frontend && npm run build`; `cd backend && .venv/bin/pytest -v --tb=short`; `cd backend && .venv/bin/ruff check .`; `cd backend && .venv/bin/mypy app`. Expected: all existing and new tests pass; integration test skips unless explicitly configured.
- [ ] **Step 5: Commit** as `feat: expose configurable Seafile adapters`.

## Self-Review

- Spec coverage: connection persistence/migration, OTP and encrypted token, explicit adapter with no fallback, both provider metadata operations, API uploads/downloads and encrypted libraries, staged transfer/cancellation/retries, SHA-256 readback, UI, docs, and CE 13.x integration path are assigned to Tasks 1–4.
- Interfaces: Task 1 defines the auth client and adapter field consumed by Tasks 2–4; Task 2 defines exact API transfer method signatures consumed by Task 3; Task 4 consumes the published contract from Tasks 1–3.
- Review focus cases are pinned to provider and transfer tests; route validation must ensure the returned upload/download URL shares the configured server hostname.
- Proportion: four tasks group code that must be reviewed together while preserving independent RED/GREEN gates.

