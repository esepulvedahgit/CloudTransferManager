# SMB NAS Migration Implementation Plan

> Approved conversational plan for inline execution. Follow TDD for each behavior; keep existing cloud-only transfer behavior intact.

**Goal:** Browse an SMB NAS share reachable from the CTM backend and worker, select one or more subfolders, and copy to existing Google Drive, MEGA, or Seafile connections, including inventories over 5,000 files.

**Architecture:** Backend and worker connect directly to SMB over the LAN or an externally managed VPN. Rclone SMB presents one configured share/base as logical `/`. A persisted, resumable manifest drives bounded inventory and per-file copy. No agent or VPN management is introduced.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL/SQLite development store, Celery/Redis, Rclone, MEGAcmd, React/TypeScript/Vite/MUI.

**Spec:** `docs/integrations/smb-nas-migration-evaluation.md`; repository constraints in `AGENTS.md`.

## Global constraints

- Rclone subprocesses always use argv lists; never `shell=True`.
- Rclone command whitelist remains `lsjson`, `copy`, `about`, `size`.
- Only COPY; never modify source data.
- Provider credentials use AES-256-GCM; frontend responses never contain saved credentials.
- Database identifiers use UUID v4; public transfer IDs retain `TRF-YYYY-NNNNNN`.
- PostgreSQL and Redis remain internal; only reverse proxy publishes ports.
- Mock vertical slice precedes real SMB/provider integration; each task follows RED → GREEN → REFACTOR.

## Review focus

- SMB traversal outside configured share/base → reject in provider and runner path tests.
- User controlled host reaching unapproved network targets → deny unless `SMB_ALLOWED_TARGETS` is configured and matches.
- Lost VPN or a mount/share temporarily returning empty → fail distinctly; never report an empty successful migration.
- Worker restart after discovery/upload boundary → idempotent manifest and destination reconciliation tests.
- SMB credentials or provider output in API/logs → secret redaction and serialization tests.

## Task 1: SMB mock vertical slice

Extend provider types, form schemas, UI section and transfer flow with a mock SMB connection. Verify admins create SMB connections, authorized users browse root/subfolders, selection reaches the existing COPY API, and destination folder names are preserved. Tests must fail first, then pass in backend and frontend suites.

## Task 2: SMB Rclone provider

Create `RcloneSmbProvider`; validate host/share/base, construct SMB config using obscure passwords, and normalize paths relative to one share/base. Add configuration driven host allowlist; absent/empty allowlist rejects real SMB connection creation and connection tests. Enforce timeouts for interactive test/list operations, terminate subprocess on timeout, and return sanitized error classes. Test config, pagination/sorting, get-item, traversal, host rules, command argv/whitelist, secret redaction and timeout cleanup.

## Task 3: Persistent manifest

Add Alembic migration, ORM entities, store methods for both PostgreSQL and in-memory/mock store: create/insert idempotently, page scan-pending directories, page copy-pending items, update outcomes atomically, and retrieve paginated report. Use 200-row batches, stable UUID cursors, unique SHA-256 of (transfer UUID + source path), counters and source/destination credential-version snapshots. Test >10,000 rows, duplicates, cursor stability, status transitions, migration upgrade/downgrade and SQLite/PostgreSQL semantics where available.

## Task 4: SMB → Google Drive executor

Dispatch SMB source before existing provider-pair flows. Discover selected roots iteratively and save each directory's children before marking it scanned. Preflight file size/file-count limits and destination mappings before writes. Copy one file at a time using Rclone; create empty folders using only allowed COPY behavior. Persist each file result and transfer counters in one transaction, then publish SSE. Test 10,001 files in bounded batches, duplicate/overlapping roots, empty folders, changed source, cancellation, transient/permanent failures, restart, ambiguous upload, destination verification and progress totals.

## Task 5: MEGA and Seafile destinations

Adapt per-file SMB staging to existing MEGAcmd and Seafile Rclone/API strategies. Enforce temporary space preflight, one file staged at a time, source-size validation, destination verification, cleanup on every exit and no duplicate byte accounting. Tests cover low disk, large file, error/cancel cleanup, and each explicit Seafile adapter.

## Task 6: resume and UI/reporting

Add paginated transfer-item report endpoint and `resume` endpoint for SMB jobs in FAILED/CANCELLED. Resume only when source/destination credential versions and SMB root match inventory snapshots; process pending/failed entries, verifying destination for in-flight entries before retry. Reject concurrent execution with existing transfer lock. Add UI for SMB root browsing, multi-select, structure preview, indeterminate scan/progress, per-item results and resume. Test authorization, CSRF, version mismatch, cancellation, resume and responsive navigation.

## Task 7: deployment/docs/acceptance

Pin a tested Rclone version with SMB support for backend and worker; document Docker DNS/routing/firewall over LAN/VPN and `SMB_ALLOWED_TARGETS`; preserve networks and staging permissions. Run migration tests, complete backend/frontend suites, Ruff, mypy, frontend lint/build, and opt-in integration tests for SMB → Google Drive, MEGA and Seafile.
