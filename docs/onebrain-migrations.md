# OneBrain Migration Discipline

OneBrain uses Alembic for Postgres schema changes. Runtime store constructors must consume an already-migrated schema; they must not create or alter tables.

## Current Rule

- Memory-mode stores may keep their local file bootstrap behavior.
- Postgres-backed store constructors must not create, alter, or drop schema.
- When `ONEBRAIN_VECTOR_STORE=pgvector`, run Alembic before starting the app.
- Store constructors must not destroy or rewrite existing customer data.
- A schema or embedding-model mismatch must fail loudly with operator guidance.
- Re-embedding is a migration, not a startup side effect.

## Local Postgres Setup

```powershell
$env:ONEBRAIN_VECTOR_STORE = "pgvector"
$env:ONEBRAIN_DATABASE_URL = "postgresql://user:password@localhost:5432/onebrain"
$env:ONEBRAIN_MIGRATION_EMBEDDING_DIM = "256"
alembic upgrade head
```

`ONEBRAIN_MIGRATION_EMBEDDING_DIM` controls the pgvector dimension used by the baseline migration. If it is not set, the migration falls back to `ONEBRAIN_EMBEDDING_DIM`, then `256`.

## Current Migration Scope

The Alembic baseline covers the current Postgres-backed app schema:

- `chunks`
- users
- conversations
- service keys
- platform accounts/spaces/app installations/audit
- intake records
- background jobs and job file payloads
- service-key lifecycle metadata

It does not create control-plane tables yet because the active control plane is still JSON-backed. Add a `PostgresControlPlaneStore` and matching migrations before moving operator release, backup, health, and rollout state into Postgres.

## Production Deployments

For production, migrations should run with an owner/migration database role before the FastAPI app starts with its app role:

1. Take or verify a database backup.
2. Set `ONEBRAIN_DATABASE_URL` for the target database.
3. Set `ONEBRAIN_MIGRATION_EMBEDDING_DIM` to match the embedding provider used by the deployment.
4. Run `alembic upgrade head`.
5. Start or roll FastAPI.
6. Run a worker process with `python -m app.deploy.start_worker` when async ingestion is enabled.

The baseline migration can adopt a compatible pre-Alembic database created by
the old runtime schema bootstrap. It uses `CREATE TABLE IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, and additive `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` statements for known legacy columns. It still refuses an existing
`chunks.embedding` vector dimension mismatch instead of rewriting customer
data.

An existing database with unknown or incompatible tables must be handled
intentionally by an operator. The application does not auto-stamp or self-heal
schema outside Alembic.

## Background Jobs

Postgres mode defaults to async ingestion. Uploads, service captures, and service intake requests can enqueue durable jobs and return a job id. Workers claim jobs from the `jobs` table and store small upload payloads in `job_files`.

Useful settings:

- `ONEBRAIN_ASYNC_INGESTION`: defaults to true for `pgvector`, false for memory mode.
- `ONEBRAIN_WORKER_POLL_SECONDS`: worker idle poll interval.
- `ONEBRAIN_WORKER_BATCH_SIZE`: number of jobs claimed per poll.
- `ONEBRAIN_JOB_MAX_ATTEMPTS`: retry limit for queued jobs.
- `ONEBRAIN_SCHEMA_WAIT_SECONDS`: deployment worker startup wait for migrated schema.
- `ONEBRAIN_SCHEMA_WAIT_POLL_SECONDS`: schema wait poll interval.

## Service-Key Lifecycle

Migration `0003_service_key_lifecycle` adds lifecycle metadata to
`service_keys`: last-used timestamp, last-used endpoint label, use count,
rotation lineage, and revoked timestamp. Successful service-key authentication
updates only coarse metadata; it never records bearer tokens, secrets, request
bodies, document text, or intake content.

Admins can rotate a service key with `POST /api/service-keys/{key_id}/rotate`.
Rotation returns the new plaintext once and revokes the old key immediately.

## Embedding Model Changes

Vector embeddings are only comparable when they come from the same model and dimension. When changing an embedding model:

1. Keep source text and metadata intact.
2. Add a new embedding version/model marker.
3. Re-embed in a background job or one-off migration.
4. Build or refresh indexes for the new vector column/table.
5. Switch retrieval only after the new embedding set is complete.
6. Retire the old embedding set after verification and backup.

The application must never drop the `chunks` table automatically to recover from a dimension mismatch.

## Later Work

- Add migration dry-run checks in CI.
- Add backup-before-migration checks for customer deployments.
- Add a release manifest field for migration ranges.
- Add Postgres-backed control-plane tables.
- Add a restricted app role and enable row-level security as a separate hardening migration.
