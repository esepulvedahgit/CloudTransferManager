# SMB NAS Migration Execution Ledger

- Approved objective: browse an SMB share reachable from the CTM backend and worker over LAN or an externally managed VPN, select NAS folders/files, and COPY them to existing cloud providers. Support inventories larger than 5,000 files.
- Approved approach: `docs/superpowers/plans/2026-10-12-smb-nas-migration.md`; approved evaluation: `docs/integrations/smb-nas-migration-evaluation.md`.
- Workspace: detached managed worktree `/root/.codex/worktrees/1df8/CloudTranferManager`, forked from `develop` at `d298c3f`.
- Original baseline: backend 252 passed / 3 skipped; frontend 29 passed. `npm ci` reported 3 pre-existing dependency audit advisories (1 moderate, 2 critical); dependency files were not intentionally changed.

## Execution

- [x] SMB connection contract, private-services section, root/subfolder navigation, folder selection and existing COPY workflow.
- [x] Rclone SMB provider with exact host allowlist, share/base logical root, obscured password, and bounded interactive commands with subprocess termination and temporary-config cleanup on timeout.
- [x] Persistent, idempotent manifest in 200-item pages, UUID cursors, credential-version snapshots, counters and paginated per-file report.
- [x] SMB inventory preflight for file count, known sizes, configured maximum size, safe paths, network availability and destination mapping collisions.
- [x] Per-file transfers to Google Drive, MEGA and both Seafile adapters; preserve empty directories, verify uploaded files and retain COPY-only source behavior.
- [x] Resume FAILED/CANCELLED SMB transfers only while connection credential versions match; reconcile interrupted uploads using persisted destination-before-attempt state and size verification.
- [x] Deployment settings and LAN/VPN/firewall setup guide. Rclone is pinned to 1.75.2 and its archive checksum is checked in the backend image build.
- [x] Fixed a pre-existing destination auto-selection issue exposed by the SMB explorer flow.
- [x] Resolved a pre-existing duplicate Alembic revision ID by chaining SMB migrations from the existing SMTP revision; SQLite upgrade, downgrade to the previous revision, and re-upgrade passed.

## Verification

- Backend: `275 passed, 3 skipped`.
- Frontend: `31 passed`; `npm run lint` and `npm run build` passed.
- Ruff: passed. Mypy: passed for 32 source files.
- `docker compose config -q`: passed using a temporary `.env` copied from `.env.example`; temporary file removed.
- `git diff --check`: passed.
- No physical NAS, external VPN route, live cloud account or built Docker image was available for an end-to-end deployment test. The skipped tests are existing opt-in/environment-dependent integration and runtime tests.
