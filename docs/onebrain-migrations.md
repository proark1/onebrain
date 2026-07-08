# OneBrain Migration Discipline

This project still has prototype bootstrap code that creates tables from store constructors. That is acceptable for local demos and synthetic data, but production data must move through explicit migrations.

## Current Rule

- Store constructors may create missing tables for local bootstrap.
- Store constructors must not destroy or rewrite existing customer data.
- A schema or embedding-model mismatch must fail loudly with operator guidance.
- Re-embedding is a migration, not a startup side effect.

## First Production Migration Target

Move Postgres-backed stores to a versioned migration tool such as Alembic before real customer data is loaded. The first migration set should cover:

- `chunks`
- users
- conversations
- service keys
- platform accounts/spaces/app installations/audit
- intake records
- control-plane tables

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

- Add Alembic or an equivalent migration runner.
- Add migration dry-run checks in CI.
- Add backup-before-migration checks for customer deployments.
- Add a release manifest field for migration ranges.
