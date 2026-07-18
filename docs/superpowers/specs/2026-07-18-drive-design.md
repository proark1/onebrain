# OneBrain Drive design

## Status

Approved product direction, captured for implementation planning. This design
supersedes the initial Drive proposal where that proposal conflicts with the
repository's current authorization, ingestion, storage, or deployment behavior.

## Goal

Make Drive the canonical home for company files in OneBrain. Filing a document
must determine both its human audience and whether OneBrain's AI may use it,
without requiring a second knowledge-structuring workflow.

The interface deliberately uses familiar file-management conventions: spaces,
My Drive, folders, breadcrumbs, **New**, **Move to**, **Manage access**,
**Download**, and **Move to trash**. The implementation remains a OneBrain core
feature rather than a pixel-level Google Drive copy or a separate deployable.

## Product principles

1. **One file home.** Drive replaces the Knowledge upload UI as the normal way
   to add files. Existing chunk-only knowledge remains visible through an
   explicitly labeled compatibility collection.
2. **Filing is policy.** A file's space, classification, location, team/category,
   personal owner, approval state, and indexing choice determine its effective
   audience and AI visibility.
3. **AI visibility is structural.** A file that is not indexed, not approved,
   trashed, unsupported, or blocked has no published chunks.
4. **Tightening is immediate.** A move, relabel, un-index, approval revocation,
   replacement, or trash operation makes the old chunks unavailable before new
   indexing work can publish.
5. **Existing retrieval authority remains authoritative.** All AI consumers keep
   using `RetrievalService` and `AccessFilter`; Drive does not add a parallel
   retrieval authorization system.
6. **Originals are governed data.** Stored bytes participate in export,
   erasure, legal holds, retention, audit, backup, restore, and capacity
   monitoring from the first customer release.
7. **Private means private in normal product use.** My Drive content is visible
   only to its owner. Privileged privacy and legal workflows remain possible
   through explicit, audited governance paths rather than normal browsing.

## Scope decomposition

Drive crosses authorization, blob storage, ingestion, governance, deployment,
and frontend concerns. It will be implemented as four ordered projects rather
than one indivisible change.

### Project A: security and storage foundations

- Member-scoped account and space discovery.
- Fail-closed space and private-owner resolution.
- Tenant-configurable department groups and member assignments, with a
  compatibility mapping for existing category-based deployments.
- Terminal `job_files` byte cleanup, legacy sweep, and privacy-erasure coverage.
- Streaming Drive blob interface and attached-volume deployment layout.
- Backup, restore, capacity, and low-disk behavior.
- Supported-format allowlist and extraction safety limits.

### Project B: Drive core

- Folder, file, and immutable revision persistence with forced RLS.
- Streaming upload sessions, download, and range requests.
- Folder navigation, metadata search, move, trash, restore, and permanent-delete
  governance.
- File-level approval and race-safe indexing publication.
- Privacy export/erase, holds, retention, tombstones, and audit integration.

### Project C: canonical customer experience

- Drive navigation, Spaces, My Drive, folders, breadcrumbs, list view, upload
  tray, AI status, search, review, download, trash, and access-label controls.
- Redirect or retire the duplicate Knowledge uploader.
- Virtual index-only collection for legacy documents without originals.

### Project D: later Drive capabilities

- Preview, version-history UI, recents, starred items, details, bulk operations,
  automatic trash purge, and reviewed legacy backfill.
- Per-person sharing, login-only expiring links, activity, and AI-assisted filing
  only after a separate security review.

Projects A through C define the first production release. Project D is not a
release dependency.

## Considered approaches

### Canonical Drive with an AI-index projection — selected

Drive owns file lifecycle and original bytes. Published chunks are a derived
projection used exclusively by the existing retrieval stack. This best matches
the product goal, provides honest download and governance behavior, and avoids
two competing upload systems.

### Thin file-tree UI over existing chunks — rejected

This is faster initially, but existing documents do not retain trustworthy
originals and cannot provide genuine download, version, trash, backup, or
complete erasure behavior. It would look like Drive without having Drive's data
lifecycle.

### Google Drive connector as the primary file home — rejected

A connector can be valuable later, but it would make an external product the
source of truth and import external ACL semantics before OneBrain has a stable
file model. It also would not give offline or self-hosted deployments a standard
OneBrain file home.

## Module boundaries

Drive is part of `onebrain_core` and runs in the existing API, worker, and
frontend containers.

- `app/drive/base.py`: domain records, enums, and protocols.
- `app/drive/memory.py`: deterministic test implementation.
- `app/drive/postgres.py`: transactional metadata persistence.
- `app/drive/blobs.py`: streaming blob protocol and local implementation.
- `app/drive/access.py`: conversion from Drive records to the existing
  `AccessFilter` metadata shape plus direct-file authorization helpers.
- `app/drive/service.py`: lifecycle orchestration and state transitions.
- `app/drive/factory.py`: configured implementations.
- `app/routers/drive.py`: authenticated HTTP surface.
- `onebrain-web/src/app/drive`: canonical server-page entry.
- Focused client components under `onebrain-web/src/components/drive`.

Drive does not receive a new optional platform `app_id`, container, Caddy host,
or service-key API in the first release. Audit records use `onebrain_core` and
the existing knowledge-management purpose where applicable.

## Access model

### Effective labels

Every file stores a flattened effective policy snapshot:

- `tenant_id`
- `account_id`
- `space_id`
- `space_kind`
- `owner_user_id` for personal spaces
- `classification`
- `location`
- `department_group_id` when the file is department-scoped
- `approval_status`
- `desired_indexed`

The first release introduces tenant-configurable department groups scoped to an
account. A group has a stable opaque ID, mutable display name, and user
memberships. A user may belong to multiple departments; a file may target zero
or one department in the first release. The stable group ID is stamped into the
chunk metadata field currently named `category`, allowing the existing
`AccessFilter` category-membership check to remain the retrieval enforcement
point while the product vocabulary becomes accurate.

Existing deployments keep their current role-derived categories as compatibility
entitlements. They are represented as read-only legacy teams until an operator
maps them to configurable department groups. The Drive UI calls them **Teams**
during that compatibility period and uses **Department** only for configured
groups. Renaming a department never changes its stable ID or requires reindexing.

### Folder defaults

A folder stores default classification, location, team/category, and
index-for-AI values. Uploading or moving a file copies those defaults onto the
file. Defaults affect new and newly moved files only.

Changing folder defaults never silently rewrites existing descendants. A later
explicit **Apply to existing contents** operation may preview and enqueue such a
bulk change.

### Direct file access

Listing, metadata reads, downloads, moves, and restores build the same metadata
shape used by `AccessFilter` and apply it to the authenticated principal. Space
membership is checked before file access. Unknown spaces, missing personal
owners, and incomplete access metadata are rejected rather than interpreted as
shared content.

RLS is the database backstop, not the only authorization layer. Filesystem blobs
are reachable only through authenticated Drive services and use opaque keys.

### Future per-person ACLs

Per-person sharing is absent from the first release. When introduced, grants may
narrow but never widen classification, location, membership, or clearance.

If a per-person ACL makes a file narrower than its label-based AI audience, the
file remains unindexed unless every AI consumer and retrieval query gains
file-level ACL enforcement. Share links are never public; future links require
login, expiry, and live authorization on every open.

## Data model

All Drive tables include tenant, account, and space scope columns, use forced
RLS, and are registered in the required-table verification list.

### `drive_folders`

- Stable folder ID and parent folder ID.
- Scope and normalized name.
- Snapshotted filing defaults.
- Created/updated actor and timestamps.
- Trash state and original parent for restore.
- Root uniqueness and sibling-name constraints.

Folder operations prevent parent cycles and enforce a configured maximum depth.

### `drive_files`

- Stable file ID, current folder, and current revision ID.
- Original display name and normalized search name.
- Flattened effective access labels.
- `desired_indexed`, approval state, and trash state.
- `index_generation`, `index_status`, and `active_doc_id`.
- Created/updated actor and timestamps.

### `drive_file_revisions`

- Immutable revision ID and owning file ID.
- Opaque storage key.
- SHA-256 content hash, size, detected media type, and upload filename.
- Upload actor and completion timestamp.
- Extraction summary and failure information without raw extracted content.

The revision table exists in the initial schema even though the version-history
UI is later. Replacing a file creates a new immutable revision and never mutates
the old blob in place.

## Blob storage and deployment

The initial `DriveBlobStore` uses the customer's attached data volume. The API
and worker receive the same dedicated mount, such as
`/mnt/onebrain-data/drive` on the host mounted at `/data/drive` in the
containers.

The interface supports:

- Staged streaming writes with an incremental SHA-256 hash and exact byte cap.
- Atomic promotion to a permanent revision key.
- Streaming and HTTP range reads.
- `stat`, exact delete, prefix cleanup, and incomplete-upload cleanup.
- Usage measurement and low-space refusal before accepting new content.

Blob keys use IDs only:

```text
drive/{tenant_id}/{account_id}/{space_id}/{file_id}/{revision_id}
```

User filenames never become filesystem paths. Folder moves never move blobs.

Database backup alone is not a valid Drive backup. The release process must
back up both metadata and blobs, record a consistency boundary, and verify a
full restore. Deployment-level storage quotas and low-disk alerts are required;
per-user quotas are not.

## Upload protocol

Drive uses a three-step protocol so the API does not buffer large multipart
bodies:

1. `POST /api/drive/uploads` validates destination metadata and creates a
   short-lived upload session.
2. `PUT /api/drive/uploads/{upload_id}/content` streams the raw request body to
   staging while hashing and enforcing the Drive-specific size cap.
3. `POST /api/drive/uploads/{upload_id}/complete` verifies size/hash, atomically
   creates the file revision, promotes the blob, and queues processing.

The global body-limit middleware recognizes the content endpoint and applies the
Drive cap. The initial cap is 250 MiB only if streaming and deployed temporary
storage behavior are verified; otherwise the release retains the existing
50 MiB cap.

Abandoned upload sessions expire and their staged bytes are deleted. Retrying
completion is idempotent. Resumable chunk uploads are deferred.

## Format and content safety

Indexing uses an explicit supported-format allowlist. PDF, DOCX, XLSX/XLSM,
PPTX, RTF, supported images, and known text formats may enter extraction.
Unknown binary and unsupported formats remain stored and downloadable with an
**Unsupported for AI** state; they never fall through to arbitrary text decode.

The extractor enforces archive expansion, page, pixel, worksheet, slide, text,
memory, and execution-time limits. Upload handling validates detected media type,
sanitizes display filenames, rejects path traversal, and does not trust the
client MIME type.

Executable or active content is not previewed inline. If malware scanning is not
available in the first release, potentially active formats are download-only and
the limitation is stated in deployment documentation.

## Indexing and approval lifecycle

Drive file metadata is the source of truth for lifecycle state. Chunks are the
published AI projection.

Every upload, replacement, move with effective-label changes, relabel, indexing
toggle, approval revocation, trash, or restore increments `index_generation`.
A worker captures the file ID, revision ID, and generation when work is queued.

The worker:

1. Reads the immutable revision from blob storage.
2. Extracts and applies content safety limits.
3. Runs the configured PII scan and classification policy.
4. Moves approval-required content into a file-level review state without
   publishing embeddings.
5. Chunks and embeds approved, supported, indexing-enabled content.
6. Locks the file row before publication.
7. Rechecks revision, generation, labels, approval, indexing choice, and trash
   state.
8. Publishes new chunks and activates the new deterministic document ID in one
   database transaction.
9. Deletes or retires the old active document in that same transition.

If any recheck fails, the work is stale and cannot publish. A tightening action
first removes or makes the old active document ineligible, then queues any
replacement projection.

User-visible states are:

- Not indexed
- Awaiting review
- Indexing
- Indexed
- Blocked
- Unsupported
- Failed

Internal states may additionally represent queued, extracting, stale, and
publication transitions.

## PII and deployment policy

The existing high-precision PII scanner is a detection floor, not proof that a
file has no personal data. Passing it must never be presented as compliance
clearance.

Production Drive storage and AI indexing are separately controlled deployment
capabilities. A customer deployment may enable:

- neither storage nor indexing;
- explicitly approved storage-only behavior; or
- approved storage and indexing.

Real-customer enablement requires the relevant privacy basis and DPIA decision.
Synthetic development remains available for implementation and release testing.

## Governance lifecycle

### Trash and deletion

Normal deletion moves an item to trash and immediately removes its active AI
projection. Restore returns it to its original accessible parent when possible,
or to the space root otherwise, then re-evaluates indexing policy.

Permanent deletion:

1. Checks legal holds at the exact file and enclosing scopes.
2. Deletes active and stale chunks.
3. Deletes every revision blob and verifies the prefix is empty.
4. Deletes or redacts Drive rows according to the governance contract.
5. Writes a content-free tombstone containing IDs, never filenames.
6. Emits an audit event.

Automatic 30-day purge is enabled only after a recurring retention scheduler is
operational. Until then, trash and restore still ship, and permanent deletion is
an explicit privileged action.

### Privacy export and erase

Privacy export includes file/folder metadata, audit-relevant lifecycle data,
and original revisions through a background-generated portable archive or
manifested download. Large originals are not embedded into the current JSON
export response.

Scope erasure covers chunks, Drive rows, blobs, pending upload sessions, jobs,
and `job_files`. Legal holds block destructive erasure where required and the
result states what was retained.

### Legacy job bytes

`job_files` bytes are retained only while an ingestion job can retry. Terminal
success and final terminal failure delete the bytes in the terminal-state
transaction. A one-time migration/sweep removes historical terminal-job bytes,
and privacy erasure includes any remaining scope-matched jobs and files.

## HTTP surface

The first release exposes authenticated, member-scoped routes under
`/api/drive`:

- Account/space roots visible to the current member.
- Folder children and breadcrumbs with cursor pagination.
- File metadata and AI state.
- Folder create, rename, move, defaults update, trash, and restore.
- File upload-session creation, streaming content, completion, rename, move,
  relabel, index toggle, trash, restore, download, and range download.
- Metadata search and filters.
- Review queue plus approve/reject actions for authorized reviewers.
- Privileged permanent delete.

Every mutation supports idempotency where retries are plausible, validates the
current version/generation, and produces an audit event. List endpoints never
return records merely because their IDs are known.

## Customer experience

### Information architecture

The console navigation gains **Drive** as the primary knowledge/file surface.
The left rail contains:

- My Drive
- Spaces available to the current member
- Needs review, when the user has review permission
- Trash
- Existing knowledge, when legacy chunk-only documents exist

The main surface contains breadcrumbs, contextual actions, metadata search and
filters, and a file table. The AI status has a dedicated compact column rather
than being hidden in a details dialog.

### Upload interaction

Users can choose **New**, drag anywhere into the current folder, or drop onto a
folder. A bottom-corner tray displays per-file progression without blocking
browsing.

The tray summarizes inherited policy in one line, for example:

> 12 files · Finance · Internal · Munich · Indexed for AI — Change

Complete ordinary folder defaults require no modal. A confirmation appears when
defaults are incomplete, indexing sensitive content needs an explicit decision,
or a move changes the audience.

### Access controls

**Manage access** shows the computed audience from membership, classification,
location, and team/category. Per-person invitations are absent in the first
release; the UI explains that access is controlled by company policy rather than
pretending the control works.

### Visual direction

Drive follows the existing OneBrain console shell and tokens. Its signature
element is the AI-status rail: a quiet, consistently aligned status treatment
that makes the relationship between file lifecycle and AI visibility legible at
a glance. Folder classification color is restrained and semantic rather than
decorative.

The interface uses a dense but calm table on desktop and a card-list adaptation
on small screens. Keyboard focus, screen-reader labels, reduced motion, drag/drop
fallbacks, and non-color status indicators are release requirements. No new UI
dependency is introduced unless the existing component stack cannot meet an
accessibility requirement.

## Error handling

- Storage full: reject before content acceptance when possible; preserve an
  actionable retry state without creating a phantom file.
- Interrupted upload: keep a short-lived retryable session record, clean staged
  bytes after expiry, and allow safe retry of completion.
- Extraction limit or unsupported type: keep the original, report the specific
  AI limitation, and publish no chunks.
- Indexing failure: preserve the previous eligible projection only when access
  did not tighten; otherwise remain structurally unindexed until retry succeeds.
- Move conflict: return the current generation and require the client to refresh
  rather than silently overwriting newer policy.
- Missing restore parent: restore to the accessible space root and tell the user.
- Held item: refuse permanent deletion and provide a non-sensitive hold reason.
- Blob/row inconsistency: fail closed for downloads and indexing, emit an
  operational alert, and do not manufacture metadata.

## Migration and compatibility

Existing documents have chunks but often no durable original. They appear in a
virtual **Existing knowledge** collection with clear **Original unavailable**
copy. They can be queried, relabeled through existing supported paths, or
removed, but cannot claim download or version behavior.

The old `/documents` frontend redirects to `/drive` after Drive reaches feature
parity. Existing document APIs remain temporarily for integrations and are
deprecated with telemetry before removal. Legacy backfill is opt-in and never
invents original files.

## Rollout

1. Ship Project A behind a disabled Drive capability.
2. Run the historical `job_files` sweep and verify privacy erasure behavior.
3. Validate blob backup/restore and disk alerts in development.
4. Ship Projects B and C behind the capability in synthetic deployments.
5. Run the full security sentinel and race suite through chat, AI Employees, and
   service queries.
6. Enable per customer only after storage, privacy, backup, and indexing policy
   approval.

Rollback disables new uploads and indexing first, while preserving governed
access to already stored originals. Additive schema changes remain in place
until data has been exported or explicitly erased.

## Verification and release gates

### Authorization and leakage

- Access-matrix tests cover tenant, account, space, private owner,
  classification, location, team/category, and approval combinations.
- Confidential, restricted, personal, not-indexed, pending, trashed, and revoked
  sentinels never appear through chat, AI Employees, service ask, or folded
  conversation history.
- Private titles, search results, download metadata, and review records do not
  leak to unrelated users or ordinary admins.
- Unknown scope and missing private owner are rejected.

### Concurrency

- Turn indexing off during extraction.
- Trash during extraction.
- Revoke approval before publication.
- Move or relabel twice and complete jobs out of order.
- Replace a file while the previous revision is embedding.
- Retry upload completion and worker publication.

Only the final eligible generation may become active.

### Storage and content safety

- Uploads are streamed without whole-file application buffering.
- Exact size caps, range reads, content hash, path traversal, disk-full behavior,
  incomplete cleanup, and corrupt/missing blob behavior are tested.
- Unsupported types are stored without chunks.
- Archive bombs and excessive page, pixel, sheet, slide, and text sizes stop at
  configured limits.

### Governance and operations

- Export contains originals and metadata in a portable form.
- Erasure removes chunks, rows, revisions, blobs, staged uploads, jobs, and job
  bytes.
- Holds block permanent deletion; tombstones remain content-free.
- Database and blob backup/restore round-trip together.
- New tables pass forced-RLS and role-separation checks.
- Development-gate end-to-end tests cover upload, review, retrieval, move,
  un-index, trash, restore, download, and erase.

## Delivery estimate

Projects A through C are estimated at 12–16 calendar weeks for one engineer,
including production verification and rollout work. A narrower 50 MiB,
admin-only, no-trash prototype would be faster but is not an acceptable standard
module and is not part of this design.
