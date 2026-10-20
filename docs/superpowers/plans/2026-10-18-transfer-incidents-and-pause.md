# Transfer Incidents and File-Boundary Pause Implementation Plan

> **For agentic workers:** Execute inline with TDD on the existing `develop` branch. Do not create a worktree. Each task is independently verified before moving to the next.

**Goal:** Surface failed and explicitly omitted files in a fast, paginated attention panel, and let supported transfers pause after the current file completes.

**Architecture:** Phase 1 adds a permission-filtered database query over failed and explicitly omitted manifest rows and a shared attention panel above the history. Phase 2 adds durable `PAUSING`/`PAUSED` control state, atomic per-file checkpoints, and pause/resume UI; current manifest-based jobs and MOCK are supported, while active legacy bulk jobs report why pause is unavailable.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Celery, Redis SSE, React, TypeScript, PostgreSQL.

**Spec:** `docs/specs/04-transfers-engine-spec.md`, `docs/specs/05-realtime-telemetry-spec.md`, `docs/integrations/transfer-scheduling.md`.

## Global Constraints

- Keep provider commands safe and preserve the COPY-only invariant.
- Omission remains explicit, manual, idempotent, and auditable; never omit automatically.
- PostgreSQL is authoritative for pause requests, state, and dispatch ownership; Redis is only a notification hint.
- Do not pause by terminating a provider process. A selected in-flight file finishes copy, verification, and staging cleanup.
- Preserve schedule date, dependencies, copied manifest rows, assignments, and attempt count across pause/resume.
- Do not pause an active legacy bulk copy that cannot prove a file boundary; return a safe availability reason.
- Integrate the changes directly into `develop`, without a worktree.

## Review Focus

- A file after thousands of manifest rows remains visible in the first incident page.
- Same basenames in distinct directories retain distinct full source and destination paths.
- Manual omission continues to reject stale attempts, transient failures, and ambiguous destination writes.
- Concurrent pause and next-file claim never starts an unclaimed subsequent file.
- Stale progress, cancellation, last-file completion, worker loss, scheduled dispatch, and dependency state cannot overwrite or bypass a pause.

---

## Phase 1 — Attention panel

### Task 1: Indexed incident query and API

Add a bounded, permission-filtered `GET /api/v1/transfer-incidents?kind=failed|omitted&limit=20&cursor=…` endpoint. Return the transfer's public state, public source/destination connection names and provider types, full manifest paths, error/omission details, skip/retry eligibility, global counts, and next cursor. Filter `FAILED` file rows and explicit omissions in SQL, sort newest first with stable UUID tie breaking, and batch-load event evidence rather than reading all events per row. Add partial indexes for failed non-directory rows ordered by `updated_at,id` and explicit omissions ordered by `omitted_at,id`.

Tests: owner/ADMIN access, cursor stability, global counts, large manifests, exact paths with Unicode and duplicate basenames, explicit omissions distinct from legacy `SKIPPED`, and skip eligibility/stale evidence.

### Task 2: Shared incident panel and fast actions

Add **Con error** and **Omitidos** tabs with paginated results, defaulting to errors when present. Mount the panel above the history in the transfer center. Include transfer identity/status/attempt, source → destination connection labels, exact full source and assigned destination paths, requested destination when renamed, safe error/omission details, and direct eligible actions. Reuse the existing confirmed manual skip-and-resume endpoint. Refresh both lists and counts after mutations and on the existing 15-second visible-page polling cadence. Keep the full per-transfer report and CSV available.

Tests: panel shows incidents outside the recent-history limit, renders complete paths without collisions, pages both tabs independently, performs confirmed skip, and refreshes when the row changes category.

## Phase 2 — Pause at file boundaries

### Task 3: Durable state and store/API transitions

Add `PAUSING` and `PAUSED` to model/database constraints and status handling, `pause_requested_at`, `paused_at`, and monotonic public `state_version`. Add permission-checked `POST /api/v1/transfers/{id}/pause`; extend resume to accept paused jobs without increasing `attempt_count`. Unstarted scheduled/queued jobs pause immediately, invalidate dispatch reservations, and preserve `scheduled_at`; resume derives `SCHEDULED` versus `QUEUED` from that date. Paused work is never dispatched or counted against active execution capacity, but remains visible and keeps its connections protected. Keep the MemoryStore behavior equivalent.

Tests: valid/invalid/repeated transitions, owner/ADMIN authorization, future and due schedules, dispatch races, capacity release, no extra attempt, cancellation precedence, dependency blocking, and `state_version` ordering.

### Task 4: File-boundary checkpoints and fenced persistence

Add one common durable checkpoint before each next manifest file across provider, SMB, and MOCK execution. Atomically check pause/cancel and claim the next file under the transfer row lock. A pause request during a selected file leaves that file running through copy, verification, and staging cleanup; then the executor exits through a pause-ready result. The worker marks `PAUSED` and releases its dispatch lease only after provider contexts and heartbeat are closed. Fence worker progress saves by dispatch token, preserve DB `PAUSING` against stale executor snapshots, publish canonical persisted state, and increment `state_version` so clients reject stale frames. A fatal file/cleanup error and normal final completion retain precedence. Active legacy bulk jobs return `LEGACY_BULK_EXECUTION`; an unstarted legacy job may pause before dispatch.

Tests: pause in source download, destination write, verification and staging cleanup; assert current file completes and next file is untouched; race pause with file claim; fatal current-file failure; last file; worker loss; cancellation; provider cleanup; repeated dispatch and stale token rejection.

### Task 5: Scheduling, UI, SSE, and operations documentation

Expose pause capability and a safe unavailable reason in REST/SSE. Display **Pausando** with the current file and **Pausada** with Resume/Cancel actions in the transfer center and Programaciones. Keep paused jobs out of active progress cards and dispatcher capacity while visible in the appropriate list. Extend status merge logic to use `state_version`, close/recover SSE correctly at pause, and allow a fresh stream after resume. Update the transfer, SSE, scheduling, and API docs.

Tests: current and background transfer controls, accessible labels, scheduled and immediate pause/resume, unsupported legacy message, stale SSE frames, refresh after restart, API/OpenAPI contracts, and documentation status names.

## Verification and rollout

- Run focused tests RED→GREEN per task, then the full backend and frontend suites, backend lint/type checks, frontend lint/build, and migration upgrade/downgrade checks on disposable PostgreSQL.
- Before each active Compose deployment, back up PostgreSQL, check that no live transfer would be interrupted, migrate once, rebuild backend/API/workers/frontend from the same commit, and verify readiness and proxy access.
- Do not downgrade while any transfer is `PAUSING` or `PAUSED`; drain or resume those jobs first.
- Verify the incident panel against real data without mutating `TRF-2026-000013`; test pause/resume on a dedicated transfer before relying on it operationally.
