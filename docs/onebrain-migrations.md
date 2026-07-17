# OneBrain Migration Discipline

OneBrain uses Alembic for Postgres schema changes. Runtime store constructors must consume an already-migrated schema; they must not create or alter tables.

## Current Rule

- Memory-mode stores may keep their local file bootstrap behavior.
- Postgres-backed store constructors must not create, alter, or drop schema.
- When `ONEBRAIN_VECTOR_STORE=pgvector`, run Alembic before starting the app.
- Store constructors must not destroy or rewrite existing customer data.
- A schema or embedding-model mismatch must fail loudly with operator guidance.
- Re-embedding is a migration, not a startup side effect.
- Production API and worker replicas must use the same migrated PostgreSQL
  schema; do not scale new code before its additive migration is applied.
- Migration owners use the owner DSN. Application replicas use the restricted
  app role after migration and must not have DDL authority.

## Local Postgres Setup

```powershell
$env:ONEBRAIN_VECTOR_STORE = "pgvector"
$env:ONEBRAIN_DATABASE_URL = "postgresql://user:password@localhost:5432/onebrain"
$env:ONEBRAIN_MIGRATION_EMBEDDING_DIM = "256"
alembic upgrade head
```

`ONEBRAIN_MIGRATION_EMBEDDING_DIM` controls the pgvector dimension used by the baseline migration. If it is not set, the migration falls back to `ONEBRAIN_EMBEDDING_DIM`, then `256`.

## Current migration scope

The Alembic baseline covers the current Postgres-backed app schema:

- `chunks`
- users
- conversations
- service keys
- platform accounts/spaces/app installations/audit
- intake records
- background jobs and job file payloads
- service-key lifecycle metadata
- account and app-level brand themes
- Mission Control deployments, releases, rollouts, backup/health records,
  fleet telemetry, promotion state, and provisioning runs
- legal holds, retention/tombstone records, and append-only platform audit
  events
- AI Employee profiles, conversations, messages, missions, runs, work
  products, connector bindings, and approval records
- shared hashed login-rate-limit counters

The active control plane is PostgreSQL-backed in production; it is not a
JSON-only operator state store. The current additive sequence is:

- `0025_provisioning_module_selection`: replaces legacy provisioning bundles
  with explicit selected module IDs;
- `0026_job_leases`: lease token and expiry fields plus claim index for durable
  background jobs;
- `0027_ai_agent_run_leases`: fenced leases and heartbeat state for direct AI
  Employee turns;
- `0028_customer_teardown_protocol`: a record-only two-person review table;
  it has no deletion executor; and
- `0029_auth_rate_limits`: shared fixed-window counters with HMAC-hashed
  subjects for all API replicas; and
- `0030_job_queue_rls_roles`: forced RLS and separate restricted PostgreSQL
  logins for request-side job status/creation versus cross-tenant worker
  claims.

## Production Deployments

For production, migrations should run with an owner/migration database role before the FastAPI app starts with its app role:

1. Take or verify a recoverable database backup and record the release rollback
   classification. A `restore_required` release needs a tested restore point.
2. Before Alembic, provision distinct non-owner PostgreSQL runtime logins. On
   a Hetzner box,
   [`deploy/box/postgres-init.sh`](../deploy/box/postgres-init.sh) configures
   the OneBrain app/worker pair plus product-local Assistant and Communication
   roles from the corresponding `POSTGRES_*_ROLE` and
   `POSTGRES_*_PASSWORD` values. The `postgres-roles` one-shot service reruns
   that idempotent normalization on every existing volume before migrations;
   each runtime role is `NOSUPERUSER NOBYPASSRLS NOINHERIT` and can connect
   only to its intended product database. Before applying this update to a
   legacy box, use Mission Control's operator-admin
   `POST /api/fleet/deployments/{id}/backfill-runtime-db-credentials` endpoint
   to add and re-seal any missing restricted runtime passwords, then wait for the bumped
   secrets epoch to appear in the box heartbeat.
3. Give the migration process its owner-role DSN plus
   `ONEBRAIN_POSTGRES_APP_ROLE` and `ONEBRAIN_POSTGRES_WORKER_ROLE`. Set
   `ONEBRAIN_MIGRATION_EMBEDDING_DIM` to the fixed configured embedding
   dimension for that vector set. Never point a rehearsal at a production
   customer database.
4. Run `alembic upgrade head` before adding new API or worker replicas.
   Revision `0030_job_queue_rls_roles` creates the queue policies, grants the
   app role normal application-table DML and sequence use, then removes broad
   queue privileges in favour of its column-level queue grants.
5. Configure API replicas with the app-role
   `ONEBRAIN_DATABASE_URL` and both role-name settings, but no
   `ONEBRAIN_WORKER_DATABASE_URL`. Configure each worker with the same app DSN
   and both names plus a distinct worker-role
   `ONEBRAIN_WORKER_DATABASE_URL`. Do not put the worker password in an API
   service environment.
6. Verify the expected revision, forced RLS, role identity, and pgvector
   schema. For production LiteLLM embeddings, verify the provider output
   dimension before accepting traffic. Finally, start or roll FastAPI, then
   run `python -m app.deploy.start_worker` when async ingestion is enabled.

The baseline migration can adopt a compatible pre-Alembic database created by
the old runtime schema bootstrap. It uses `CREATE TABLE IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, and additive `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` statements for known legacy columns. It still refuses an existing
`chunks.embedding` vector dimension mismatch instead of rewriting customer
data.

An existing database with unknown or incompatible tables must be handled
intentionally by an operator. The application does not auto-stamp or self-heal
schema outside Alembic.

Do not use a downgrade as a routine recovery mechanism after customer traffic
has used a new schema. Roll back code only when the release's rollback kind
permits it; otherwise restore the recorded backup into the approved recovery
workflow.

## Background Jobs

Postgres mode defaults to async ingestion. Uploads, service captures, and service intake requests can enqueue durable jobs and return a job id. Workers claim jobs from the `jobs` table and store small upload payloads in `job_files`.

Each claim carries a random, expiring lease token. Workers heartbeat while a
handler is active, stop claiming during shutdown, and may mark success, retry,
or failure only while their token is current. An expired lease can be reclaimed
within the attempt budget, so every external handler effect must be idempotent
by job ID. Operators should investigate lease-loss and retry-exhaustion alerts
rather than manually writing a terminal status over a new owner.

### Job queue database role split

`0030_job_queue_rls_roles` is required for production queue use. The app role
can enqueue within its transaction-local tenant/account/space scope and read
only client-safe job status fields. It has no `SELECT` privilege on
`jobs.payload` or `job_files`, and it cannot update or delete jobs. The worker
role can read uploaded files and claim/update queue lease fields across tenants,
but has no insert/delete access, no payload/scope-column update access, and no
general application-table privileges.

Use this rollout order exactly:

1. Create or normalize the two login roles before Alembic, as described above.
   The app and worker names must be simple, distinct PostgreSQL identifiers.
2. Run `alembic upgrade head` as the database owner with both
   `ONEBRAIN_POSTGRES_*_ROLE` names set. Do not run `0030` as either restricted
   login.
3. Start API replicas with only the app login. Start worker replicas with their
   own worker login in `ONEBRAIN_WORKER_DATABASE_URL`; their regular
   `ONEBRAIN_DATABASE_URL` remains the app login for non-queue stores.
4. Prove the boundary with both logins: the app login can create/read a
   same-scope job but receives a privilege error for `SELECT payload FROM jobs`
   and for any `job_files` read; it sees no job in another tenant scope. The
   worker login can claim that job and read its file, but cannot query a normal
   application table or update `jobs.payload`.

The migration establishes default privileges only for objects created by the
owner that ran it. Future migrations must keep that owner (or explicitly set
matching default privileges for a new owner), preserve the app role's normal
table/sequence grants, and add queue columns only with deliberate
column-specific grants. Rotate a role password by changing the Postgres role
with the owner login and then updating the corresponding service secret; merely
changing an environment value does not update a pre-existing login role.

Useful settings:

- `ONEBRAIN_ASYNC_INGESTION`: defaults to true for `pgvector`, false for memory mode.
- `ONEBRAIN_WORKER_POLL_SECONDS`: worker idle poll interval.
- `ONEBRAIN_WORKER_BATCH_SIZE`: number of jobs claimed per poll.
- `ONEBRAIN_JOB_MAX_ATTEMPTS`: retry limit for queued jobs.
- `ONEBRAIN_SCHEMA_WAIT_SECONDS`: deployment worker startup wait for migrated schema.
- `ONEBRAIN_SCHEMA_WAIT_POLL_SECONDS`: schema wait poll interval.

## Shared login rate limits

Migration `0029_auth_rate_limits` adds `auth_rate_limits`, which production
API replicas use for fixed-window failed-login counters. The database stores
only an HMAC hash of the normalized account identifier or client address plus
the scope, count, and expiry. Set a unique
`ONEBRAIN_LOGIN_RATE_LIMIT_SECRET` of at least 32 characters before production
startup.

Client addresses come from the direct peer by default. Configure
`ONEBRAIN_TRUSTED_PROXY_CIDRS` and `ONEBRAIN_TRUSTED_PROXY_HOPS` only when the
peer is a controlled proxy; arbitrary forwarding headers must not choose a
client's rate-limit key. Verify the same lockout is observed through at least
two API replicas after every proxy or replica-topology change.

## Direct AI turn leases

Migration `0027_ai_agent_run_leases` gives direct AI Employee turns the same
fenced ownership model. A reconnect with the same idempotency key replays or
continues the existing safe state; it does not silently start a second paid
provider call. Provider timeout must leave time to heartbeat the configured
turn lease. A terminal write with a stale token is rejected.

## Record-only teardown review

Migration `0028_customer_teardown_protocol` is an audit/review protocol, not a
resource-deletion feature. It persists the deployment/account binding, evidence
references, requester, two distinct approvers, nonce hash, expiry, and terminal
non-execution state. Active legal holds block review. The raw nonce is returned
only at request creation, and a completed approval state says
`execution_disabled`; it never authorizes a Hetzner or broker delete call.

## Service-Key Lifecycle

Migration `0003_service_key_lifecycle` adds lifecycle metadata to
`service_keys`: last-used timestamp, last-used endpoint label, use count,
rotation lineage, and revoked timestamp. Successful service-key authentication
updates only coarse metadata; it never records bearer tokens, secrets, request
bodies, document text, or intake content.

Admins can rotate a service key with `POST /api/service-keys/{key_id}/rotate`.
Rotation returns the new plaintext once and revokes the old key immediately.

## Brand Theme Provisioning

Migration `0004_brand_theme_provisioning` adds `platform_brand_themes`.
Themes are account-scoped with an optional `app_id`, allowing a customer
default plus assistant or communication overrides. Colors are normalized hex
tokens and are used by provisioning, the Next.js operator UI, and service-key
theme resolution.

## Embedding Model Changes

Vector embeddings are only comparable when they come from the same model and
dimension. `ONEBRAIN_EMBEDDING_DIM` is a fixed contract with the migrated
pgvector column; a LiteLLM provider response must match it exactly. In a
production LiteLLM + pgvector deployment, startup probes the provider and
validates the schema dimension before traffic is served. When changing an
embedding model or dimension:

1. Keep source text and metadata intact.
2. Add a new embedding version/model marker.
3. Re-embed in a background job or one-off migration.
4. Build or refresh indexes for the new vector column/table.
5. Switch retrieval only after the new embedding set is complete.
6. Retire the old embedding set after verification and backup.

The application must never drop the `chunks` table automatically, silently
change the configured dimension, or accept a provider-selected dimension to
recover from a mismatch.

## Operational evidence still required

The migrations are additive, but each production activation still needs
operator evidence outside this repository: a backup/restore rehearsal, a
two-replica shared-login-limit check, a job/AI lease-recovery exercise, an
embedding provider/schema preflight, and a tenant-isolation check. See the
[production activation runbook](production-activation-runbook.md) for the
required sequence.
