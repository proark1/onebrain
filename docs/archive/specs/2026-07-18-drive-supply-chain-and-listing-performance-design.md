# Drive scanner supply-chain and listing performance design

Date: 2026-07-18
Status: approved design
Scope: OneBrain Core worker packaging and customer Drive read paths

## 1. Outcome

This change closes two follow-up gaps in the standard Drive module:

1. Every operating-system package used to build, run, or validate the malware
   scanner resolves from an immutable Debian snapshot under an explicit lock.
   The image baseline definitions are also an immutable, checksum-verified
   input instead of the result of a live `freshclam` call.
2. Listing one or one hundred Drive files performs a constant number of
   revision and malware-evidence reads. Authorization, ordering, pagination,
   quarantine behavior, and the public API remain unchanged.

The release image digest remains the deployment and rollback authority. This
design guarantees immutable build inputs and reproducible dependency
resolution; it does not claim byte-identical OCI output across different
builders, timestamps, or compression implementations.

## 2. Non-goals

- No new deployed service, package mirror, database table, or API endpoint.
- No denormalized malware status on `drive_files`.
- No change to Drive pagination, result ordering, access labels, or RLS.
- No bulk job-write API. One idempotent indexing job per eligible file remains
  intentional; only its prerequisite metadata reads are batched.
- Runtime ClamAV definition refresh remains online and signed. Only the image
  baseline becomes a locked build input.
- CI tools that cannot affect worker image bytes are outside the package lock.

## 3. Chosen approach and rejected alternatives

### Chosen: checked-in supply-chain lock plus Debian snapshots

A checked-in lock describes the digest-pinned Python base image, Debian suite,
snapshot timestamps, exact direct package versions for each Docker stage,
expected package-inventory hashes, and the immutable baseline-definition
artifact. Docker stages replace their default APT sources, install exact
versions, and verify the resulting inventory before proceeding.

This keeps the normal worker image and release flow while turning dependency
updates into reviewed lock changes.

### Rejected: dedicated scanner base image

A separately published scanner-runtime image could hide most package work
behind one digest. It would add another release artifact, promotion pipeline,
attestation relationship, and rollback lifecycle without improving the
customer runtime boundary.

### Rejected: vendored `.deb` closure

Committing or privately hosting every package gives fully offline builds but
adds large binary storage, mirror signing, garbage collection, and an extra
security-update process. The selected networked snapshot is immutable while
remaining operationally lighter.

## 4. Worker supply-chain lock

### 4.1 Lock contract

`deploy/scanner-sandbox/worker-supply-chain.lock.json` is the single
source-controlled contract. It contains:

- schema version;
- exact `python:3.12-slim@sha256:...` base reference;
- Debian suite and architecture;
- independent Debian and Debian Security snapshot timestamps and URLs;
- the Debian archive keyring path used for signature verification;
- exact direct package names and versions for `scanner-launcher-build`,
  `worker-runtime`, and `scanner-validation`;
- the canonical full `dpkg-query` inventory hash expected after each stage;
- the baseline-definition artifact URL, SHA-256, identity, and archive-manifest
  SHA-256; and
- a human-readable update timestamp that is evidence only and never used for
  package resolution.

The base digest is duplicated in `Dockerfile.worker` because Docker requires it
in `FROM`. A static verifier fails if the Dockerfile and lock disagree.

### 4.2 Debian sources and signature verification

Every APT-using stage deletes or disables the base image's default Debian
sources before `apt-get update`. A source-controlled helper writes deb822
sources that point only to the locked paths under `snapshot.debian.org`.

Historical snapshots use `Check-Valid-Until: no`; normal signature validation,
TLS certificate validation, and `Signed-By` remain mandatory. The build never
uses `trusted=yes`, `allow-unauthenticated`, or disabled date/signature checks.

Every direct install is rendered as `package=version`. A missing snapshot,
expired TLS chain, unavailable version, signature failure, or dependency
conflict stops the build. There is no fallback to `deb.debian.org`,
`security.debian.org`, or a floating package version.

### 4.3 Closure verification

After each stage installs its packages, the helper writes the normalized
inventory with:

```text
dpkg-query -W -f='${binary:Package}=${Version}\n' | LC_ALL=C sort
```

The build compares its SHA-256 with the stage hash in the lock. This catches a
changed transitive dependency, base-image/package mismatch, architecture
change, or accidental extra package even when all direct versions still
resolve.

The final runtime inventory continues to be stored at
`/opt/onebrain/scanner-packages.txt`. Scanner release evidence additionally
records the lock-file SHA-256, Debian snapshot identities, base-image digest,
and verified inventory SHA-256.

### 4.4 Immutable image baseline definitions

The production image build no longer runs live `freshclam`. A project-owned
GitHub release asset in `proark1/onebrain` contains only the official `main`,
`daily`, and `bytecode` databases plus its canonical archive manifest. The tag
is `scanner-definitions-<identity>` and the asset is
`onebrain-clamav-definitions-<identity>.tar.gz`. Its versioned URL, content
SHA-256, identity, and manifest SHA-256 are in the supply-chain lock. The
checksum, rather than the mutability policy of the hosting service, is the
build's authority.

A source-controlled fetcher:

1. accepts only the HTTPS URL recorded in the lock;
2. streams into a temporary file with a size ceiling;
3. verifies the archive SHA-256 before extraction;
4. rejects absolute paths, traversal, links, devices, duplicate members, and
   unexpected filenames;
5. verifies every extracted member against the locked canonical manifest; and
6. hands the resulting directory to the existing `sigtool`, definition
   manifest, EICAR, and scanner-capability gates.

The release asset is never silently overwritten. Publication uses two protected
changes so a not-yet-published URL never enters the production build graph:

1. merge generator/tooling or package-snapshot changes to protected `main`
   while retaining the currently valid definition artifact;
2. dispatch the approval-gated `scanner-definitions` publication workflow at
   that exact protected-main HEAD;
3. create and verify an immutable release whose source tooling and
   package/snapshot projection are byte-bound to that commit; and
4. merge a follow-up lock activation from the generated fragment.

The publisher deliberately excludes the active `definitions` object from its
generator-lock projection; otherwise step 2 would circularly require the new
artifact to exist before publication. It remains fail-closed for drafts,
orphan tags, target/body/asset drift, extra assets, or mutable releases. An
exact already-immutable terminal state is verification-only and reruns both
attestations without mutation. Runtime `freshclam` refresh and atomic
definition-set rotation remain unchanged.

For the initial rollout, where no prior OneBrain artifact exists, delivery must
be split once more: land the generator/publisher and bootstrap target on
protected main while retaining the pre-scanner production worker, publish the
first immutable asset, then land the scanner production graph and its generated
lock activation. The combined development tree is not releasable while its
first locked asset is absent.

### 4.5 Update and rollback process

Dependency refreshes are explicit maintenance changes:

1. choose a new snapshot and direct versions;
2. generate candidate inventories in clean Docker stages;
3. merge those generator inputs while retaining a valid active definition;
4. publish and attest a new baseline from the protected workflow;
5. activate the generated artifact in a follow-up lock change;
6. run scanner sandbox, clean-file, EICAR, encrypted-content, archive-limit,
   package-inventory, and evidence verification; and
7. promote the resulting digest through the normal signed release gate.

Security updates are reviewed at least monthly and immediately for relevant
critical vulnerabilities. Prior locks and definition assets remain available
for audit and rebuilds. Customer rollback still selects the previous signed
worker digest and never consults APT. If a persisted runtime definition set is
incompatible with the rolled-back engine, startup must reseed the packaged
baseline or remain unavailable; it must not accept the incompatible set.

## 5. Drive list-detail batch

### 5.1 Domain contract

Add a bounded `DriveFileListDetail` value containing an exact
`DriveRevision | None` and its authoritative current-policy
`DriveMalwareScan | None`. `DriveEntryPage` gains a read-only mapping keyed by
the file snapshot's `current_revision_id`.

`DriveStore` gains:

```python
get_file_list_details(
    *,
    account_id: str,
    space_id: str,
    revision_ids: Sequence[str],
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
) -> Mapping[str, DriveFileListDetail]
```

The method rejects an oversized input, deduplicates IDs, and returns no entry
for missing or out-of-scope revisions. It never accepts file names, arbitrary
SQL clauses, or caller-supplied tenant scope.

### 5.2 Authorization order

`DriveService.list_entries` resolves and authorizes the account/space once. It
uses that already-resolved space kind and personal owner to run the existing
file AccessFilter over each page row. Only current-revision IDs belonging to
authorized rows enter the batch store call.

This removes the existing repeated platform account, membership, and space
lookups without weakening the Python authorization layer. PostgreSQL RLS and
explicit account/space predicates remain the database backstop.

Pagination continues to occur before Python audience filtering. A page may
therefore remain shorter than the requested limit while its cursor advances;
this change does not silently alter that established contract.

### 5.3 PostgreSQL implementation

PostgreSQL performs one scoped query for the bounded revision-ID array. It
selects exact revision rows and uses a `LEFT JOIN LATERAL` to select the
authoritative malware attempt for the requested policy epoch ordered by
`attempt_sequence DESC, id DESC LIMIT 1`.

The query includes account and space predicates on both revision and scan
records. Existing RLS remains forced. The existing authoritative-scan index
supports the lateral lookup. Result construction verifies that scan file ID,
revision SHA-256, and revision size match the immutable revision before the
detail can be treated as clean.

The main page query remains fixed-cost. Its folder count, folder page, file
page, and one detail query are constant regardless of the number of returned
files.

### 5.4 Memory implementation

The memory store takes one lock, resolves requested revisions directly, and
makes one pass over malware attempts to choose the maximum
`(attempt_sequence, id)` for each requested revision and current policy epoch.
It does not call the existing scalar scan-list method once per revision.

Memory and PostgreSQL return identical results for duplicate IDs, missing
revisions, old policy epochs, multiple attempts, cross-space IDs, and mismatched
evidence.

### 5.5 Service reuse and serialization

The batch detail mapping is attached only after authorization. The service
passes the same mapping to queued-index reconciliation. `_enqueue_index` accepts
an optional exact detail and avoids scalar revision and scan reads when that
detail is present. Reconciliation constructs one bounded, same-scope
`JobEnqueueSpec` collection and invokes `JobStore.enqueue_many` once for the
page. Each file remains its own durable job row, keyed exactly as authoritative
scan completion keys it:
`drive-ingest:{file_id}:{revision_id}:{generation}`. It does not receive its own
queue call. The queue row ID is also derived from that tuple as
`job_<md5("onebrain:" + key)>`; this is an opaque deterministic identifier, not
a security checksum. The Python domain helper and PostgreSQL completion
transaction use the same derivation, so listing repair and scan completion
cannot race to claim one idempotency key with different job IDs.

The generic byte-free batch contract requires a non-empty idempotency key,
rejects mixed tenant/account/space scopes, and is capped at the Drive maximum
page size. Memory resolves the whole collection under one lock. PostgreSQL uses
one RLS-scoped connection and transaction with a fixed three execute calls: one
scope statement, one `jsonb_to_recordset` set insert with `ON CONFLICT DO
NOTHING`, and one set resolution query in ordinal order. Keeping insertion and
resolution as separate statements is intentional: under `READ COMMITTED`, the
second statement sees an already-existing or concurrently committed unique-key
winner without granting the request role queue-update privileges. The store
commits only when every input ordinal resolves.

Review lists use the same service-level authorization and batch-detail helper.
Single-file mutation responses may use a one-item batch. Router serialization
receives a file plus its detail and performs no store or service calls.

The serializer exposes download only when `is_clean_attestation(revision,
scan)` succeeds for that exact file snapshot. Missing revision, missing scan,
old policy evidence, SHA/size mismatch, or concurrent revision replacement
produces `rescan_required`, zero/unknown presentation metadata, no download URL,
and no indexing job. Internal storage keys and threat details remain absent.

## 6. Failure behavior

- Snapshot, version, signature, inventory, artifact, or manifest drift fails the
  Docker build.
- No build path falls back to a live package repository or live baseline
  definition download.
- A batch-detail database failure fails the listing request rather than
  fabricating a clean state.
- Missing or inconsistent rows fail closed per file as `rescan_required`.
- Index-queue batch failure does not fail browsing. The transaction rolls back
  rather than partially committing the page, and durable `queued` metadata
  retries the bounded batch on a later read.
- Authorization filtering happens before metadata batching, so hidden revision
  identifiers never enter the detail query.

## 7. Verification and acceptance criteria

### Supply chain

- Static tests reject live Debian sources, unversioned direct installs,
  mismatched base/index-platform digests, hidden BuildKit mount dependencies,
  remote `ADD`, disabled signature checks, and APT use outside the locked
  helper.
- Lock parsing rejects unknown fields, duplicate packages, malformed versions,
  non-HTTPS artifact URLs, invalid digests, and missing stage inventories.
- Each Docker stage verifies its normalized package inventory.
- Artifact tests reject checksum mismatch, traversal, links, duplicate or
  unexpected members, and manifest mismatch.
- The exact packaged image still passes sandbox probes, clean-file, EICAR,
  encrypted archive, limit, capability, and release-evidence checks.
- The production stage graph cannot reach the definition bootstrap or online
  runtime-refresh targets; final validation never invokes `freshclam`.
- Image publication is callable only from the fully gated test workflow. The
  definition publisher runs only from protected-main `workflow_dispatch` with
  the approval environment and exact generator-input binding.
- CI performs a cold-cache worker build using only the locked sources and
  verifies the lock and evidence identities inside the resulting image.

### Drive listing

- One and one hundred visible files execute one batch-detail query and one space
  authorization sequence.
- Unauthorized files' revision IDs never reach the batch store method.
- PostgreSQL selects the exact latest attempt for the current policy epoch with
  one execute call for arbitrary bounded input.
- Memory performs one scan pass and matches PostgreSQL semantics.
- One and one hundred eligible queued files invoke `enqueue_many` once per page;
  they never invoke scalar queue enqueue from the listing path.
- A 100-item PostgreSQL enqueue batch uses one connection and exactly three
  execute calls for both new rows and already-existing authoritative
  `drive-ingest:` rows, returns stable IDs, and leaves exactly 100 durable jobs.
- A 100-item memory enqueue batch takes one lock; mixed scopes, oversized input,
  or an unresolved job-ID collision fail atomically.
- Listing-first crash-window repair and scan completion resolve the same exact
  deterministic ingestion job ID and never create a duplicate.
- Normal, review, bootstrap, trash, and single-file response serializers make
  no persistence calls.
- Queued clean files reuse batch details and reconcile as one idempotent batch
  without scalar metadata or queue operations; quarantined or mismatched files
  never enqueue.
- Existing response fields, ordering, cursors, and access-control tests remain
  unchanged.

## 8. Release gate

The change is releasable only after the native worker-image build and scanner
smoke run on Linux and the live PostgreSQL suite proves query and RLS behavior.
Local unit tests may validate contracts on platforms without Docker or
PostgreSQL, but they do not replace those two CI gates.
