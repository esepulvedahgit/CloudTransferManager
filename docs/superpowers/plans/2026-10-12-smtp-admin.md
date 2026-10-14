# SMTP Administration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let administrators save, test, and use SMTP settings from a new subtab in the existing Administration dialog, with credentials encrypted at rest.

**Architecture:** Add one global SMTP settings record to the existing repository contract. Keep host, port, TLS, and sender as ordinary configuration fields; encrypt username and password together using the existing AES-256-GCM envelope cipher and master key. Add admin-only API routes, resolve persisted settings in the notification worker with environment fallback before first save, and add a matching MUI subtab.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, Alembic, existing `CredentialCipher`, Celery, React, TypeScript, MUI, TanStack Query, React Hook Form, Zod, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-12-smtp-admin-design.md`

## Global Constraints

- SMTP settings are global and only `ADMIN` may read, test, or update them.
- Usernames and passwords are encrypted together with AES-256-GCM envelope encryption using `MASTER_ENCRYPTION_KEY_FILE`.
- The frontend never receives saved SMTP credentials; empty credential inputs preserve the stored values.
- Internal database identifiers are UUID v4.
- SMTP errors and audit metadata never contain credentials, raw server replies, or unsanitized exception details.
- SMTP delivery remains a separate Celery task; an SMTP failure never changes a successful transfer to failed.
- With no saved database row, current SMTP environment variables and Docker Secret password file remain the fallback.

## Review Focus

- Invalid host, port, and sender values must be rejected before storage or network access; pin with API validation tests.
- A partial username/password pair must not replace valid saved credentials; pin with API update tests.
- An SMTP authentication, TLS, or timeout failure must return a safe generic message; pin with connection tests.
- Missing database configuration must preserve environment fallback, including password-file behavior; pin with resolver tests.
- A worker using a saved database configuration must deliver email without leaking credentials or changing transfer state on delivery failure; pin with worker tests.

---

### Task 1: Persist one encrypted SMTP settings record

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/services/memory_store.py`
- Modify: `backend/app/services/database_store.py`
- Create: `backend/migrations/versions/20261012_0004_smtp_settings.py`
- Create: `backend/tests/test_smtp_settings_storage.py`

**Interfaces:**
- Produces `SMTPSettingsRecord(id: UUID, host: str, port: int, use_tls: bool, from_address: str, encrypted_credentials: bytes)` in `memory_store.py`.
- Both repositories implement `aget_smtp_settings() -> SMTPSettingsRecord | None` and `asave_smtp_settings(record: SMTPSettingsRecord) -> SMTPSettingsRecord`.
- The SQL table has a UUIDv4 primary key and a unique scope fixed to `global`, enforcing one record without a fixed UUID.

- [ ] **Step 1: Write failing repository tests** `test_memory_store_persists_smtp_settings` and `test_database_store_persists_smtp_settings`; assert read-after-create, update replaces fields, encrypted bytes round-trip unchanged, and update preserves the record UUID.
- [ ] **Step 2: From `backend/`, run** `pytest tests/test_smtp_settings_storage.py -v`; confirm the repository methods/table are missing.
- [ ] **Step 3: Implement** the record, SQLAlchemy table, memory storage, and database upsert/read methods. Add the Alembic migration with unique `scope='global'` and UUIDv4 id.
- [ ] **Step 4: From `backend/`, run** `pytest tests/test_smtp_settings_storage.py -v` and `pytest tests/test_migrations.py -v`; expect both to pass.
- [ ] **Step 5: Commit** as `feat: persist global SMTP settings`.

### Task 2: Add encrypted credential handling and admin SMTP API

**Files:**
- Create: `backend/app/services/smtp_config.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_smtp_settings_api.py`
- Modify: `docs/api/openapi-contracts.md`
- Modify: `docs/specs/06-audit-notifications-spec.md`

**Interfaces:**
- `SMTPServerConfig(host, port, username, password, from_address, use_tls)` is the internal delivery/test value object in `smtp_config.py`.
- `encrypt_smtp_credentials(settings: Settings, settings_id: UUID, username: str, password: str) -> bytes` and `decrypt_smtp_credentials(settings: Settings, record: SMTPSettingsRecord) -> tuple[str, str]` use `CredentialCipher` and the system master key.
- `test_smtp_connection(config: SMTPServerConfig) -> None` performs SMTP greeting, optional STARTTLS, optional authentication, and clean quit; it raises only a sanitized `SMTPConnectionError` to callers.
- API routes are `GET /api/v1/settings/smtp`, `PUT /api/v1/settings/smtp`, and `POST /api/v1/settings/smtp/test`.
- Safe GET/PUT response shape is `{host, port, use_tls, from_address, credentials_configured}`. PUT and test accept those fields plus optional `username` and `password`; test responds `{success, message}`. No response contains credentials.

- [ ] **Step 1: Write failing API tests** `test_smtp_settings_routes_require_admin`, `test_smtp_settings_response_never_returns_credentials`, `test_smtp_update_encrypts_and_preserves_omitted_credentials`, `test_smtp_test_uses_draft_without_persisting`, `test_smtp_settings_audit_excludes_credentials`, `test_smtp_update_rejects_invalid_fields`, and `test_smtp_test_returns_generic_failure`; assert USER gets 403, saved secrets never appear in JSON, ciphertext differs from plaintext, blank/omitted fields preserve ciphertext, test leaves storage untouched, and no error/audit body contains either credential.
- [ ] **Step 2: From `backend/`, run** `pytest tests/test_smtp_settings_api.py -v`; confirm the routes and service are missing.
- [ ] **Step 3: Implement** request validation (`port` 1–65535, valid sender address, username/password both supplied or both omitted), key-file encryption, safe responses, environment fallback, and the SMTP test helper. The test route evaluates the unsaved draft and never sends a message or persists it.
- [ ] **Step 4: From `backend/`, run** `pytest tests/test_smtp_settings_api.py -v`; expect all API and secret-handling cases to pass.
- [ ] **Step 5: Update** the API contract and notification spec with route schemas, permission behavior, safe credential handling, and the `SMTP_SETTINGS_UPDATED` audit action.
- [ ] **Step 6: Commit** as `feat: add secure SMTP administration API`.

### Task 3: Use saved configuration in notification workers

**Files:**
- Modify: `backend/app/services/smtp_config.py`
- Modify: `backend/app/services/notifications.py`
- Modify: `backend/app/worker.py`
- Modify: `backend/tests/test_worker.py`
- Create: `backend/tests/test_smtp_delivery.py`

**Interfaces:**
- `async def load_smtp_server_config(store: Any, settings: Settings) -> SMTPServerConfig` returns saved, decrypted configuration when present, otherwise environment configuration.
- `send_transfer_email(settings: Settings, smtp: SMTPServerConfig, recipient: str, transfer: Any, source_name: str, destination_name: str) -> None` uses the supplied runtime configuration.

- [ ] **Step 1: Write failing tests** `test_saved_smtp_config_takes_precedence_over_environment`, `test_smtp_config_falls_back_to_environment`, `test_smtp_delivery_uses_starttls_and_auth`, and `test_worker_passes_resolved_smtp_config`; assert selected host/account, STARTTLS-before-login order, and delivery uses the database values.
- [ ] **Step 2: From `backend/`, run** `pytest tests/test_smtp_delivery.py tests/test_worker.py -v`; confirm the worker still only reads static environment settings.
- [ ] **Step 3: Implement** the resolver and pass its result from `_send_notification` into `send_transfer_email`; preserve safe delivery exceptions and task retry behavior.
- [ ] **Step 4: From `backend/`, run** `pytest tests/test_smtp_delivery.py tests/test_worker.py -v`; expect the new precedence and delivery cases to pass.
- [ ] **Step 5: Commit** as `feat: deliver transfer mail with admin SMTP settings`.

### Task 4: Add the SMTP subtab and form

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Admin dialog tab values are `general` and `smtp`; existing user, connection-access, and audit content remains under `general`.
- Frontend API types contain only non-secret SMTP fields and `credentials_configured`; the UI does not store or query saved credentials.
- The save body may include new `username` and `password`; empty values mean preserve the server-side credential.
- Fetch and invalidate the TanStack Query key `['admin-smtp-settings']` after a successful save.

- [ ] **Step 1: Write failing component tests** `shows_admin_smtp_subtab`, `loads_smtp_settings_without_credentials`, `saves_smtp_settings_and_preserves_blank_credentials`, `tests_unsaved_smtp_draft`, and `shows_smtp_loading_success_and_error_states`; assert exact tab/form labels, never prefill the password, request payload behavior, and visible outcome alerts.
- [ ] **Step 2: From `frontend/`, run** `npm test -- src/App.test.tsx`; confirm the new tab and actions are absent.
- [ ] **Step 3: Implement** the tab navigation, SMTP form schema, TanStack Query loading/invalidation, mutation states, and consistent MUI styling. Use `type="password"`, leave saved secret inputs blank, and explain the preserve behavior.
- [ ] **Step 4: From `frontend/`, run** `npm test -- src/App.test.tsx`; expect the new component cases to pass.
- [ ] **Step 5: From `frontend/`, run** `npm run lint` and `npm run build`; expect TypeScript checks and production build to pass.
- [ ] **Step 6: Commit** as `feat: add SMTP settings tab to administration`.

### Task 5: Review integrated behavior and documentation

**Files:**
- Review: `docs/superpowers/specs/2026-10-12-smtp-admin-design.md`
- Review: `docs/api/openapi-contracts.md`
- Review: `docs/specs/06-audit-notifications-spec.md`
- Review: implementation files from Tasks 1–4

- [ ] **Step 1: From `backend/`, run** `pytest -v --tb=short`; expect the backend suite to pass.
- [ ] **Step 2: From `frontend/`, run** `npm test`, `npm run lint`, and `npm run build`; expect all to pass.
- [ ] **Step 3: Review** the diff for secret leakage, admin authorization on all three routes, audit metadata sanitization, environment fallback, responsive tab layout, and consistency with all seven acceptance criteria in the design spec.
- [ ] **Step 4: Commit** any focused integration corrections with an appropriate `fix:` message.
