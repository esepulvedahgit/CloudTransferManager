# Connection Archiving Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Give administrators a dedicated connections section where they can archive a connection only after its active transfers finish, while retaining transfer history.

**Architecture:** Keep connection rows as tombstones using nullable `deleted_at`, erase saved ciphertext and grants on archive, and retain transfer foreign keys. Serialize archiving with transfer creation by locking affected connection rows in deterministic order. Add an admin-only interface tab with confirmation and refresh behavior.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, Alembic, pytest, React 18, TypeScript, MUI, TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-11-connection-archiving-design.md`

**Workspace note:** The current workspace already has unrelated modified files, and the existing `backend/` and `frontend/` trees are untracked. Preserve that state; do not stage or commit those trees as feature commits. Keep the implementation reviewable in the workspace until a clean commit boundary is established.

## Global Constraints

- Only `ADMIN` may archive a connection; mutable requests retain CSRF protection.
- Archiving changes only application configuration and never invokes a provider or removes remote files.
- The archived row remains so transfer history retains its required connection references.
- Archived connections have `deleted_at TIMESTAMP WITH TIME ZONE NULL`, no saved ciphertext, and no user grants.
- Block archiving for `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING`, or `CANCELLING` transfers; return `409 Conflict`.
- Archived connections are hidden from connection lists and unavailable to navigation, tests, OAuth, edits, grants, and new transfers; return `404` for archived or missing connection IDs.
- Never return or log saved provider credentials. The `CONNECTION_ARCHIVED` audit metadata contains only non-secret connection facts.
- Keep the storage operation within the existing `COPY`-only MVP and Rclone command allowlist.

## Review Focus

- An active transfer in any of the five specified states blocks archive without changing the row; a terminal transfer does not block it. Pin both cases in Task 1 repository tests.
- Archive racing with transfer creation leaves either a new active transfer and an archive conflict, or an archived connection and no new transfer. Pin both valid outcomes in Task 1's SQL concurrency test.
- A successful archive clears credentials and grants but retains the connection row, terminal transfer references, and transfer events. Pin this in Task 1 memory and SQL tests and Task 2 API tests.
- Non-admin or missing-CSRF archive requests cannot mutate a connection. Pin this in Task 2 API tests.
- A stale selected source or destination is removed after archive, and a `409` leaves the confirmation available with the server reason. Pin this in Task 3 UI tests.

---

## File Map

- `backend/app/models.py`: add the nullable archive timestamp to `StorageConnection`.
- `backend/migrations/versions/20261011_0002_connection_archiving.py`: add and remove the timestamp in migration upgrade/downgrade.
- `backend/app/services/memory_store.py`: add `deleted_at`, archive and availability errors, and matching in-memory rules.
- `backend/app/services/database_store.py`: map archive state, filter unavailable connections, and implement transactional archive/transfer/grant behavior.
- `backend/app/main.py`: add the admin archive route, audit event, and archived-connection handling across existing operations.
- `backend/tests/test_connection_archiving.py`: cover memory-store and API archive behavior.
- `backend/tests/test_migrations.py`: verify the full migration chain matches the current SQLAlchemy schema.
- `backend/tests/test_database_store.py`: cover SQL persistence, history retention, and archive/transfer serialization.
- `docs/specs/02-storage-providers-spec.md` and `docs/api/openapi-contracts.md`: document archive semantics and endpoint contract.
- `frontend/src/App.tsx`: add an admin-only `Conexiones` tab, inventory, confirmation flow, and selection cleanup.
- `frontend/src/styles.css`: add desktop rows, mobile cards, clear status treatments, and reduced-motion behavior.
- `frontend/src/App.test.tsx`: cover role visibility, successful archive, conflict handling, and explorer refresh.

## Task 1: Archive persistence and transfer serialization

**Files:**
- Modify: `backend/app/models.py`
- Create: `backend/migrations/versions/20261011_0002_connection_archiving.py`
- Modify: `backend/app/services/memory_store.py`
- Modify: `backend/app/services/database_store.py`
- Create: `backend/tests/test_connection_archiving.py`
- Modify: `backend/tests/test_migrations.py`
- Modify: `backend/tests/test_database_store.py`

**Interfaces:**
- Produce `ConnectionRecord.deleted_at: datetime | None` and `StorageConnection.deleted_at: Mapped[datetime | None]`.
- Produce `ConnectionInUseError` and `ConnectionUnavailableError` in `memory_store.py`, imported by `database_store.py` and `main.py`. `ConnectionUnavailableError.reason` is `"not_found"` for missing/archived IDs and `"inactive"` for inactive connections.
- Produce `await store.aarchive_connection(connection_id: UUID) -> ConnectionRecord | None`; return `None` for missing/already archived IDs and raise `ConnectionInUseError` if an associated transfer is active.
- `await store.acreate_transfer(...) -> TransferRecord` raises `ConnectionUnavailableError` when either connection is missing, archived, or inactive. The API will translate that to `404` for unavailable IDs and `409` for inactive records.
- `await store.agrant_connection(...)` raises `ConnectionUnavailableError` for an archived/missing connection.

- [x] **Step 1: Write the failing memory-store tests**

Add tests that archive an idle connection and assert `deleted_at` is set, ciphertext is empty, its grants are gone, the row is excluded from lists/access checks, and an existing terminal transfer plus its events remain. Parameterize `QUEUED`, `VALIDATING`, `RUNNING`, `RETRYING`, and `CANCELLING` and assert each raises `ConnectionInUseError` without changing the connection. Assert terminal statuses such as `COMPLETED`, `FAILED`, and `CANCELLED` do not block archive. Assert new transfers and grants reject archived connections.

- [x] **Step 2: Run the memory-store tests and verify they fail**

Run: `cd backend && pytest -v --tb=short tests/test_connection_archiving.py`
Expected: FAIL because archive state and archive operations do not exist.

- [x] **Step 3: Add archive state and the migration**

Add nullable timezone-aware `deleted_at` to `StorageConnection` and `ConnectionRecord`. Create revision `20261011_0002` with `down_revision = "20261010_0001"`; upgrade adds the nullable column and downgrade drops it. Existing rows remain available with `deleted_at = NULL`. Update the migration consistency test to apply all revisions in order, compare the resulting schema to `Base.metadata`, and downgrade revisions in reverse order.

- [x] **Step 4: Implement matching memory-store rules**

Implement `aarchive_connection`, filter archived records from `list_connections`/`aget_connection`/`ahas_connection_access`, prevent archived records from being saved or granted, and validate both connection records before `acreate_transfer`. Keep each in-memory check-and-mutate sequence free of `await` points so it is atomic within the event loop.

- [x] **Step 5: Run memory-store tests and verify they pass**

Run: `cd backend && pytest -v --tb=short tests/test_connection_archiving.py`
Expected: PASS for all archive, active-state, terminal-history, and access assertions.

- [x] **Step 6: Write failing SQL-store and concurrency tests**

Extend `tests/test_database_store.py` to verify archive persistence across reopening the store, ciphertext/grant removal, retained transfer rows/events, active-state conflict behavior, and unavailable archived connections. Add a PostgreSQL integration race test using `TEST_DATABASE_URL` (SQLite ignores `FOR UPDATE`): start archive and `acreate_transfer` concurrently against one connection pair and assert the only allowed outcomes are archive conflict with a persisted transfer, or successful archive with no persisted transfer referencing the archived connection. Keep the ordinary SQL-store assertions runnable against the existing SQLite test database.

- [x] **Step 7: Run the SQL-store tests and verify they fail**

Run: `cd backend && pytest -v --tb=short tests/test_database_store.py`
Expected: FAIL because the SQL model/store do not yet archive or serialize these operations.

- [x] **Step 8: Implement transactional SQL archive and availability checks**

In `DatabaseStore.aarchive_connection`, select the connection `FOR UPDATE`; while holding the lock, check associated transfers in the five active states, set `deleted_at` and `encrypted_credentials = b""`, delete all `UserConnectionGrant` rows for the connection, then commit. Return `None` for absent/already archived rows. In `acreate_transfer`, lock the distinct source/destination rows ordered by UUID, validate that both exist, have `deleted_at IS NULL`, and have status `ACTIVE`, then insert and commit the job in the same transaction. Apply archived filters in `aget_connection`, `alist_connections`, and `ahas_connection_access`; lock and validate the connection in `agrant_connection` so a grant cannot race with archive. Ensure `asave_connection` cannot restore an archived row from a stale record.

- [x] **Step 9: Run repository tests and verify they pass**

Run: `cd backend && pytest -v --tb=short tests/test_connection_archiving.py tests/test_database_store.py`
Expected: PASS for repository tests; when `TEST_DATABASE_URL` points to PostgreSQL, the integration test also passes with one of the two allowed concurrency outcomes. CI must run this integration test against PostgreSQL 16+.

- [x] **Step 10: Review the persistence slice without staging existing workspace files**

Run `git diff --check` and inspect `git status --short`. Keep the slice unstaged because those backend paths currently belong to the pre-existing untracked project tree.

## Task 2: API behavior, audit, and contracts

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_connection_archiving.py`
- Modify: `docs/specs/02-storage-providers-spec.md`
- Modify: `docs/api/openapi-contracts.md`

**Interfaces:**
- Consume Task 1's `aarchive_connection`, `ConnectionInUseError`, and `ConnectionUnavailableError` interfaces.
- Produce `DELETE /api/v1/connections/{connection_id}`: ADMIN plus `csrf_guard`; `204` on successful archive; `404` for missing/already archived IDs; `409` when active transfers exist.
- Successful requests record audit action `CONNECTION_ARCHIVED`, resource type `connection`, resource ID, and metadata containing only `provider_type` and `is_global`.

- [x] **Step 1: Write failing endpoint and route tests**

Cover successful admin archive with CSRF, non-admin rejection, missing-CSRF rejection, missing/already-archived IDs returning `404`, active transfer returning `409` without mutation, and success audit metadata containing no credential fields. Verify transfer history/events remain. Verify archived IDs return `404` from item listing, test, OAuth start and callback, update, grant, and transfer creation; grant/transfer races are backed by the Task 1 repository checks.

- [x] **Step 2: Run the API tests and verify they fail**

Run: `cd backend && pytest -v --tb=short tests/test_connection_archiving.py`
Expected: FAIL because the archive route and audit action are absent.

- [x] **Step 3: Implement the archive endpoint and operation guards**

Add the CSRF-protected admin `DELETE` route. Map `ConnectionInUseError` to `409` and `None` to `404`; write the audit event inside the same storage transaction as the archive. Ensure existing connection routes treat archived IDs as unavailable and translate `ConnectionUnavailableError` into the documented `404`/`409` responses, including the race-safe transfer and grant paths. Do not include credential bytes in response or audit metadata.

- [x] **Step 4: Update the storage and API specifications**

Document `deleted_at`, credential/grant removal, active statuses, retained history, and the `DELETE` response/error contract in both specified documents. State that archived IDs cannot be used by other connection operations.

- [x] **Step 5: Run API tests and backend checks**

Run: `cd backend && pytest -v --tb=short tests/test_connection_archiving.py tests/test_user_management.py tests/test_connection_credentials.py tests/test_api_security.py`
Expected: PASS, including auth, CSRF, archived-operation guards, and prior connection behavior.

Run: `cd backend && ruff check .`
Expected: no lint findings.

Run: `cd backend && mypy app`
Expected: no type errors.

- [x] **Step 6: Review the API and contract slice without staging existing workspace files**

Run `git diff --check` and inspect `git status --short`. Keep the slice unstaged; the specification and API contract files already contain unrelated workspace edits.

## Task 3: Admin connections section and archive interaction

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Add an admin-only top-level tab named `Conexiones`; retain the current `Origen`/`Destino` mobile tabs inside `Explorador`.
- Add `ConnectionManagementSection` in `App.tsx`, receiving current `Connection[]`, loading/error state, and callbacks to add or archive a connection.
- The archive callback sends `DELETE /api/v1/connections/{id}`, invalidates `['connections']`, clears matching source/destination IDs and paths, clears source selections when needed, and shows a success notice.

- [x] **Step 1: Write failing UI tests**

Add tests that an ADMIN can open `Conexiones`, see connection name/provider/status/scope/last-test data, and open the add form from the section. Confirm archive shows the selected name/provider and consequences, sends no request before confirmation, then sends `DELETE`, refreshes the list, and clears a matching explorer selection. Assert a USER never sees the `Conexiones` tab. Return a `409` response with an active-transfer reason and assert the dialog remains open, shows the reason, and disables duplicate submission while pending.

- [x] **Step 2: Run the targeted UI tests and verify they fail**

Run: `cd frontend && npm test -- --run src/App.test.tsx`
Expected: FAIL because the admin section and archive flow are absent.

- [x] **Step 3: Add admin navigation and connection inventory**

Add the `Explorador`/`Conexiones` top-level tab for ADMIN only. Render the existing explorer and its mobile source/destination tabs only in the explorer view. Move the admin `Agregar conexión` action into the connections section header. Show loading, error, and empty states; render desktop rows and mobile cards with identical connection metadata and a labeled secondary `Archivar` action.

- [x] **Step 4: Add confirmation, conflict, and refresh behavior**

Before sending, show a confirmation dialog naming the connection and explaining that stored credentials are cleared, history is retained, and the connection leaves the explorer. Disable repeat submission while pending. On `409`, keep the dialog open and show the response detail. On success, close it, invalidate `['connections']`, clear any matching source/destination selection and paths, clear selected source items, and show a short success notice.

- [x] **Step 5: Add responsive and accessible visual treatment**

In `styles.css`, make inventory rows readable on desktop and switch to cards at tablet widths through `920px` and mobile widths at or below `767px`. Keep hover feedback on hover-capable pointers. Preserve visible keyboard focus, accessible button names, status text that does not rely on color, touch-sized actions, brief interaction feedback, and `prefers-reduced-motion` behavior.

- [x] **Step 6: Run UI tests and frontend checks**

Run: `cd frontend && npm test -- --run src/App.test.tsx`
Expected: PASS, including role visibility, successful refresh/selection cleanup, and conflict handling.

Run: `cd frontend && npm run lint`
Expected: no lint findings.

Run: `cd frontend && npm run build`
Expected: production build succeeds.

- [x] **Step 7: Review the interface slice without staging existing workspace files**

Run `git diff --check` and inspect `git status --short`. Keep the slice unstaged because the frontend tree is part of the pre-existing untracked project tree.

## Final verification

- [ ] Run the migration against the configured database: `cd backend && alembic upgrade head`.
- [x] Run the complete backend suite: `cd backend && pytest -v --tb=short`.
- [x] Run backend static checks: `cd backend && ruff check .` and `cd backend && mypy app`.
- [x] Run the complete frontend suite: `cd frontend && npm test`.
- [x] Run frontend lint and production build: `cd frontend && npm run lint` and `cd frontend && npm run build`.
- [x] Review `git diff --check` and confirm the migration has no default secrets and no existing transfer rows are modified.
- [ ] Run the archive/transfer concurrency integration test against PostgreSQL 16+ with `TEST_DATABASE_URL` configured.
