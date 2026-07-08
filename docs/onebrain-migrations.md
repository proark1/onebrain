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

It does not create control-plane tables yet because the active control plane is still JSON-backed. Add a `PostgresControlPlaneStore` and matching migrations before moving operator release, backup, health, and rollout state into Postgres.

## Production Deployments

For production, migrations should run with an owner/migration database role before the FastAPI app starts with its app role:

1. Take or verify a database backup.
2. Set `ONEBRAIN_DATABASE_URL` for the target database.
3. Set `ONEBRAIN_MIGRATION_EMBEDDING_DIM` to match the embedding provider used by the deployment.
4. Run `alembic upgrade head`.
5. Start or roll FastAPI.

An existing database that has tables but no Alembic stamp must be handled intentionally by an operator. The application does not auto-stamp or self-heal schema.

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
