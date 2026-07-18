# OneBrain Drive implementation plan

**Date:** 2026-07-18
**Status:** Approved for implementation
**Design:** `docs/superpowers/specs/2026-07-18-drive-design.md`

## Objective

Ship Drive as an always-installed `onebrain_core` capability and make it the
canonical customer file-upload and AI-learning surface. Keep each technical
boundary replaceable—metadata store, blob store, lifecycle service, router,
worker, and frontend—without making Drive an optional deployment module.

## Delivery rule

The first production release comprises the security/storage foundation, Drive
core APIs, background indexing lifecycle, canonical customer UI, governance
wiring, deployment storage, and release-gate tests. Preview, per-person sharing,
share links, activity, and AI-assisted filing remain later projects.

## Implementation order

### 1. Lock the always-on module contract

**Files**

- Add `app/drive/__init__.py` and `app/drive/base.py`.
- Modify `app/config.py`.
- Add `tests/test_drive_contracts.py`.

**Test first**

- Drive status, approval, and upload-session states reject unknown values.
- Scope and access-label records normalize deterministically.
- Drive configuration has safe size, session-expiry, depth, and storage defaults.
- Drive is not added to optional app-installation or provisioning-module lists.

**Implement**

- Define immutable folder, file, revision, upload-session, and list-result
  contracts plus store/blob protocols.
- Define the lifecycle state machine and generation-conflict error.
- Add Drive configuration under `ONEBRAIN_DRIVE_*` while keeping Drive enabled as
  part of Core.

### 2. Add Drive metadata persistence and RLS

**Files**

- Add `app/drive/memory.py`, `app/drive/postgres.py`, and
  `app/drive/factory.py`.
- Add `migrations/versions/0032_onebrain_drive.py` after the identity/queue
  foundation migration reserved as `0031_drive_foundations.py`.
- Modify `app/db/rls.py` and `app/deploy/runtime.py` where runtime validation
  enumerates required Core tables.
- Add `tests/test_drive_store.py`, `tests/test_drive_migration.py`, and RLS tests.

**Test first**

- Folder/file/revision/upload-session CRUD is scope-bound and cursor-paginated.
- Sibling names, roots, parent cycles, and maximum depth are enforced.
- Generation-checked updates reject stale writes.
- Privacy-scope listing/deletion cannot cross tenant/account/space boundaries.
- All Drive tables enable and force RLS with scoped `USING` and `WITH CHECK`
  policies.

**Implement**

- Add `drive_folders`, `drive_files`, `drive_file_revisions`, and
  `drive_upload_sessions` with opaque IDs and additive indexes.
- Add account-scoped `platform_access_groups` and
  `platform_access_group_memberships`; enrich authenticated human principals
  with stable group IDs so the same entitlements reach Drive, chat, and AI
  Employee retrieval.
- Keep current revision, active document, lifecycle state, and flattened access
  labels on `drive_files`.
- Keep immutable blob identity and content facts on revisions.

### 3. Add the streaming blob boundary

**Files**

- Add `app/drive/blobs.py`.
- Modify `app/drive/factory.py` and `app/deps.py`.
- Add `tests/test_drive_blobs.py`.

**Test first**

- Staged writes stream incrementally and enforce the exact byte cap.
- Completion verifies size and hash, then atomically promotes the staged blob.
- Range reads, stat, exact delete, prefix cleanup, and expired staging cleanup
  work without trusting filenames.
- Path traversal and invalid opaque IDs are rejected.
- Low free space refuses a new upload before permanent metadata is created.

**Implement**

- Use a `DriveBlobStore` protocol independent from the backup object-store
  interface.
- Implement local storage below `/data/drive` by default.
- Use only validated tenant/account/space/file/revision IDs in storage keys.

### 4. Expose member-scoped Drive roots

**Files**

- Add `app/drive/access.py`.
- Modify `app/auth/account_access.py`, `app/platform/scope.py`, and `app/deps.py`
  only where shared member-scoping needs a fail-closed primitive.
- Add `tests/test_drive_access.py`.

**Test first**

- Account owners and active members see only eligible spaces.
- Space-specific memberships do not reveal sibling spaces.
- My Drive is owner-only in product listing, metadata, review, and download.
- Unknown spaces and unresolved private owners fail closed.
- File metadata is evaluated through the existing `AccessFilter` semantics.

**Implement**

- Add member-visible root discovery without weakening admin-only platform
  management routes.
- Build effective file metadata for direct Drive authorization.
- Correct the existing unresolved-space helper so missing scope never becomes a
  non-private label.

### 5. Implement Drive lifecycle service

**Files**

- Add `app/drive/service.py`.
- Modify `app/deps.py`.
- Add `tests/test_drive_service.py`.

**Test first**

- Folder defaults snapshot onto uploads and moves.
- Folder-default edits do not rewrite existing descendants.
- Rename, move, relabel, index toggle, replace, trash, and restore increment the
  generation where required.
- Tightening removes the active AI document before any replacement is queued.
- Restore selects the original accessible parent or the space root.
- Permanent delete is hold-checked, blob-complete, tombstoned, and audited.

**Implement**

- Keep orchestration outside routers and persistence implementations.
- Make user retries idempotent and generation-aware.
- Represent unsupported and failed indexing as file states while preserving the
  original revision.

### 6. Add upload, browse, search, download, and mutation APIs

**Files**

- Add `app/routers/drive.py`.
- Modify `app/main.py`, `app/http_limits.py`, and `app/schemas.py` only where
  shared API contracts require it.
- Add `tests/test_drive_api.py` and request-limit tests.

**Test first**

- Upload creation validates scope and destination before accepting bytes.
- Raw `PUT` content streams under a Drive-specific limit.
- Completion is idempotent and creates one revision/job.
- Completion recovers safely across the filesystem/SQL boundary: an upload is
  either still staged, permanently promoted and represented by one revision, or
  marked for deterministic reconciliation—never silently orphaned.
- Browse/search/download/range/move/trash/restore apply member and file access.
- Review approval enforces four-eyes and cannot expose private file titles.
- Direct file authorization evaluates the file's labels with a synthetic
  approved publication status; pending lifecycle state is checked separately so
  authorized uploaders/reviewers can manage a file without making its chunks
  retrievable.
- Service keys cannot access Drive in the first release.

**Implement**

- Add `/api/drive/bootstrap` as one member-scoped initial response containing
  roots, selected/default root, its first child page, counts, and capabilities;
  add focused folder/item listing, metadata search, upload-session, raw content,
  completion, file/folder mutation, review, and download endpoints for later
  interactions.
- Apply the Drive body cap only to upload-content routes; retain the global limit
  elsewhere.
- Version the bootstrap contract as `contract_version: 1` and return
  server-issued action capabilities so the browser never infers authorization
  from role IDs.

### 7. Add race-safe Drive indexing jobs

**Files**

- Modify `app/jobs/base.py` and `app/jobs/handlers.py`.
- Add focused helpers under `app/drive/indexing.py`.
- Modify `app/ingest/extract.py` and `app/ingest/pipeline.py` only to separate
  safe preparation from publication where necessary.
- Add `tests/test_drive_indexing.py` and worker tests.

**Test first**

- Pending, blocked, unsupported, not-indexed, and trashed files publish zero
  chunks.
- Turn-off, trash, approval revoke, replacement, and out-of-order moves cannot
  publish stale generations.
- Approved work stamps tenant/account/space/private-owner/access labels exactly
  once and activates only the final deterministic document ID.
- Unknown binary formats never fall through to plain-text decoding.

**Implement**

- Add `drive_file_ingest` jobs containing IDs and generation, never file bytes.
- Extract and scan before approval; embed only approved eligible files.
- Recheck the locked file generation immediately before publication and discard
  stale results.
- Publish through a Drive metadata-store unit of work that locks `drive_files`,
  rechecks generation, deletes the old chunk projection, inserts the new chunks,
  and activates `active_doc_id` in one PostgreSQL transaction. The existing
  `IngestPipeline` may supply reusable preparation helpers but is not the Drive
  publication authority.
- Keep the first release cap at 50 MiB. Raise it only after extractors accept
  file-backed bounded input rather than converting the whole original to bytes.

### 8. Close legacy `job_files` lifecycle gaps

**Files**

- Modify `app/jobs/base.py`, `app/jobs/memory.py`, and `app/jobs/postgres.py`.
- Add `migrations/versions/0031_drive_foundations.py` before the Drive schema;
  it owns department-group identity plus job-file lifecycle RLS/cleanup, and the
  migration chain remains strictly linear.
- Modify `app/routers/privacy.py` and related platform-store privacy helpers.
- Modify `tests/test_jobs.py`, `tests/test_privacy.py`, and job RLS tests.

**Test first**

- Terminal success and final failure delete job bytes transactionally.
- Lease exhaustion during `claim()` also deletes bytes once the job becomes
  terminal.
- Retryable jobs retain bytes until their next attempt.
- Historical terminal rows lose bytes during migration without deleting job
  status history.
- Scope erasure deletes jobs and job files without crossing scope boundaries.

**Implement**

- Add explicit terminal-byte cleanup to both stores.
- Add the narrow worker/app DELETE policies and grants needed for terminal
  cleanup and scope erasure without widening normal job reads.
- Sweep terminal job files once in PostgreSQL. Because deleting historical
  bytes is a restore-required migration, ship it with an explicit tested restore
  point and preserve every job history row.
- Include job records in privacy deletion summaries and audit output.

### 9. Wire governance for Drive originals

**Files**

- Extend `app/drive/service.py` and store contracts.
- Modify `app/routers/privacy.py`, `app/retention/service.py`, and platform audit,
  hold, and tombstone calls where necessary.
- Add `tests/test_drive_governance.py`.

**Test first**

- Export inventories metadata and original revisions without embedding large
  bytes into JSON.
- Erasure deletes chunks, rows, revisions, blobs, staged sessions, and jobs.
- Exact and enclosing legal holds block permanent deletion.
- Tombstones contain IDs and counts, never filenames or extracted text.
- Retention skips held records and records a retention run.

**Implement**

- Add Drive to privacy and retention domain dispatch.
- Provide a manifest-based original export contract for the first release.
- Keep every destructive path audited and idempotent.

### 10. Put Drive storage on the attached customer volume

**Files**

- Modify `app/provisioning/hetzner/render.py`.
- Add real box backup/restore scripts and timers, then modify update scripts and
  deployment documentation to install and exercise them.
- Add provisioning/render and operations tests.

**Test first**

- API and worker mount the same dedicated Drive directory from the attached data
  volume.
- The directory is created only after the volume is mounted.
- The attached volume has a persistent systemd mount unit (or equivalent fstab
  entry) ordered before Docker; startup refuses to use an unmounted
  `/mnt/onebrain-data` directory on the root disk.
- Backup and restore include Drive originals and preserve ownership.
- Backup readiness is not inferred from configuration fields or comments: a
  generated script, installed timer, retention behavior, and restore command are
  required and tested.
- Customer stacks receive Drive configuration automatically; Mission Control
  receives no customer Drive surface.

**Implement**

- Mount `/mnt/onebrain-data/drive` into Core API/workers at `/data/drive`.
- Persist the Hetzner volume device mount across host reboots and gate customer
  services on `mountpoint -q /mnt/onebrain-data` before creating or chowning the
  Drive directory.
- Back up PostgreSQL and Drive blobs under one run manifest with checksums and a
  documented consistency boundary; install the scheduled runner on customer
  boxes.
- Add storage capacity/low-disk telemetry configuration.
- Document and test database-plus-blob restore consistency.

### 11. Build the canonical Drive frontend

**Files**

- Add `onebrain-web/src/app/drive/page.tsx`.
- Add focused modules under `onebrain-web/src/features/drive/`, with direct
  imports rather than a barrel: types, server/client transport, pure state and
  presentation helpers, app/browser/sidebar/toolbar/list/status/upload/dialog
  components, icons, and a colocated CSS module.
- Modify navigation, workspace provider, `/documents`, and `globals.css`.
- Add frontend tests.

**Test first**

- Drive is a standard navigation item for signed-in customer users.
- Roots, folders, breadcrumbs, search, table/card rows, upload tray, AI status,
  review, trash, restore, and download render accessibly.
- Uploads do not block browsing and show inherited policy before completion.
- My Drive and private names never appear without API authorization.
- `/documents` transitions to Drive without breaking legacy deep links.

**Implement**

- Follow the existing console shell and token system.
- Keep server auth and initial data on the server page; isolate interaction state
  in small client components and avoid serial fetch waterfalls.
- Suppress the global admin-only Workspace selector on Drive; Drive owns its
  member-visible root rail.
- Use XHR only for raw upload-body progress; keep metadata requests on fetch and
  downloads as authenticated same-origin links so originals never enter browser
  JavaScript memory.
- Use the AI-status rail as the single visual signature; preserve responsive,
  keyboard, reduced-motion, and non-color status behavior.

### 12. Add the legacy knowledge compatibility collection

**Files**

- Extend Drive router/service read models.
- Modify the old Documents route/page behavior.
- Add API and frontend compatibility tests.

**Test first**

- Existing chunk-only documents appear as **Existing knowledge**.
- They state **Original unavailable** and expose no fake download/version action.
- New uploads cannot enter through a second competing UI.

**Implement**

- Project authorized `list_documents` results into a virtual read-only Drive
  collection.
- Redirect the customer `/documents` page to `/drive`; retain backend endpoints
  temporarily for integrations.

### 13. Run security, race, governance, and UI release gates

**Files**

- Add a Drive sentinel integration suite.
- Extend architecture, OpenAPI, RLS, deployment, and full-stack tests.
- Update operator/customer documentation.

**Verify**

- No confidential, restricted, private, not-indexed, pending, trashed, or
  revoked sentinel leaks through chat, AI Employees, service ask, or folded
  history.
- Stale workers cannot republish tightened access.
- Upload/download is streamed, bounded, and traversal-safe.
- Export, erase, holds, retention, backup, and restore cover originals.
- Backend tests, frontend tests, lint, typecheck, production build, migration
  lint, and focused provisioning tests pass.

## Done criteria

- Drive ships with every OneBrain Core customer deployment and is not selectable
  as an optional module.
- Code ownership remains modular across domain, store, blob, service, router,
  worker, and frontend boundaries.
- Every new upload has a durable original, explicit effective audience, and
  deterministic AI lifecycle.
- Files excluded from AI have no published chunks.
- My Drive and shared spaces are member-scoped from the first release.
- Drive is the canonical upload surface; legacy knowledge is represented
  honestly.
- Originals are fully covered by governance and operations.
