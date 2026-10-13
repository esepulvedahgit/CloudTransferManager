# README Architecture Documentation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Update the root README to describe the implemented application and provide an accurate Docker deployment guide with a branded architecture diagram.

**Architecture:** Keep the README as the main operator guide. Generate a self-contained architecture HTML source and an exported SVG for the rendered README image. Base every service and storage-path detail on the current Compose file and implementation; use the frontend theme for diagram styling.

**Tech Stack:** Markdown, Docker Compose, SVG/HTML, Diagram Design skill.

**Spec:** `docs/specs/07-infrastructure-docker-spec.md`, `docs/specs/04-transfers-engine-spec.md`, `docs/specs/01-auth-users-spec.md`, `docs/integrations/transfer-scheduling.md`, `docs/integrations/smb-nas-setup.md`.

## Global Constraints

- Only the reverse proxy publishes host ports; PostgreSQL, Redis, API and workers stay on private Docker networks.
- Provider credentials are encrypted in PostgreSQL and are never returned to the frontend.
- Rclone temporary configuration is private and removed after each operation; transfers use COPY semantics.
- PostgreSQL, Redis and application logs use named persistent volumes; runtime secrets and transfer temporary files are ephemeral.
- SMB reachability and the target allowlist are configured by the operator; CTM does not create or manage VPN connectivity.

## Review Focus

- Published ports and proxy networks must match `compose.yaml`; verify the documented host port defaults and LAN firewall guidance.
- Durable volumes must be distinguished from tmpfs mounts and the ephemeral staging directory.
- Secret generation must not overwrite existing keys; document backup and access requirements without exposing values.
- Staging capacity must account for the largest file plus configured reserve/margin and current concurrency.
- Google OAuth callback and SMB allowlist/network routes must match the deployed URL and reachable provider addresses.

---

### Task 1: Refresh README application overview and deployment guide

**Files:**
- Modify: `README.md`

- [x] Describe implemented authentication, providers, explorer, transfer scheduling/progress/history, and security boundaries.
- [x] Expand Docker deployment with networks, published ports, secrets, TLS, OAuth, SMB, persistence, backup and upgrade considerations.
- [x] Document `/run/ctm-secrets`, `/var/lib/cloud-transfer/tmp`, `/var/lib/cloud-transfer/staging`, container `/tmp`, and named volumes accurately.
- [x] Review every referenced relative path and command against the repository.

### Task 2: Add the application-styled architecture diagram

**Files:**
- Create: `docs/architecture.html`
- Create: `docs/architecture.svg` (exported from the HTML source)
- Modify: `README.md` (embed the SVG)
- Create: `.diagram-design` and a local Diagram Design profile only after the user approves the proposed token mapping.

- [x] Use the Architecture type, `doc-inline` size preset, and no more than 8 components and 10 connections.
- [x] Show browser/proxy, frontend, API, Celery with Rclone/MEGAcmd, PostgreSQL, Redis, and cloud/NAS providers.
- [x] Embed the SVG with a relative Markdown image link and include a short plain-text description.
- [x] Confirm SVG accessibility metadata, relative links, and diagram geometry; do not run application test suites for this documentation-only change.

## Execution record

- The final review corrected the documented Nginx redirect target: the default HTTP listener redirects to standard port `443`, while Compose publishes TLS on host port `8443` by default. The local generated certificate covers only `localhost` and `127.0.0.1`; LAN access requires a certificate with the appropriate SAN.
- The diagram represents the actual progress path: workers publish events to Redis and the API forwards them to clients over SSE. The SVG was re-exported from the final HTML source.
- Validation: Diagram Design offline self-check passed; exported SVG includes inline accessibility title/description; `git diff --check` passed. Application test suites were not run because this change only updates documentation and the diagram.
