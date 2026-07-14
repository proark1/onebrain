# OneBrain Alembic Migrations Design

## Summary

Add Alembic as the required migration system for OneBrain's Postgres-backed backend. This changes Postgres schema ownership from runtime store constructors to explicit, versioned migrations.

This is a production-readiness slice for the Python/FastAPI backend. The backend remains Python. Next.js remains the console frontend. Alembic becomes the authority for database shape whenever `ONEBRAIN_VECTOR_STORE=pgvector`.

The first implementation must not keep a Postgres schema fallback. If a Postgres-backed store is used before migrations have been applied, the app should fail clearly and tell the operator to run Alembic.

## Goals

- Add Alembic configuration and a first baseline migration for the current Postgres schema.
- Make Postgres schema creation explicit and versioned.
- Remove runtime `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE`, `CREATE INDEX`, and `CREATE EXTENSION` bootstrapping from Postgres store constructors.
- Add a clear schema verification path so the app fails loudly when the database has not been migrated.
- Keep the existing data model and table names stable.
- Preserve the existing pgvector dimension mismatch guard.
- Document the migration workflow for local development and production deployment.

## Non-Goals

- Do not rewrite the backend in TypeScript.
- Do not change the Next.js frontend in this slice.
- Do not add background workers in this slice.
- Do not redesign retrieval, memory, or task behavior in this slice.
- Do not migrate the control-plane JSON store to Postgres in this slice.
- Do not enable row-level security by default in this slice.
- Do not add data migrations for changing embedding providers or embedding dimensions.
- Do not remove memory-mode stores used by local development and tests.

## Current State

Postgres-backed schema is currently created by store constructors:

- `app/store/pgvector.py`
  - `vector` extension
  - `chunks`
  - `chunks_doc_id_idx`
  - `chunks_tenant_idx`
- `app/users/postgres.py`
  - `users`
- `app/conversations/postgres.py`
  - `conversations`
  - `messages`
  - `conv_scope_idx`
  - `conv_scope_space_idx`
  - `msg_conv_idx`
- `app/servicekeys/postgres.py`
  - `service_keys`
  - `service_keys_tenant_idx`
- `app/platform/postgres.py`
  - `platform_accounts`
  - `platform_spaces`
  - `platform_app_installations`
  - `platform_audit_events`
  - platform account/app/audit indexes
- `app/intake/postgres.py`
  - `intake_records`
  - `intake_records_scope_idx`

`docs/onebrain-migrations.md` already states the desired direction: production data must move through explicit migrations, and embedding-model changes must be handled as migrations rather than startup side effects.

`scripts/enable_rls.sql` documents a row-level-security policy for `chunks`, but it also documents an operational blocker: safe RLS requires a separate owner/migration role and restricted app role. That role split is not implemented yet.

The control plane is currently JSON-backed through `app/controlplane/memory.py`; there is no Postgres control-plane store to migrate in this slice.

## Recommended Approach

Use a mandatory Alembic baseline:

1. Add Alembic to Python dependencies.
2. Add `alembic.ini`.
3. Add `migrations/env.py`.
4. Add a first revision such as `0001_baseline_onebrain_schema`.
5. Move the current Postgres schema into that revision.
6. Remove schema creation from Postgres store constructors.
7. Add schema validation helpers used by Postgres-backed stores.
8. Update documentation and tests.

This approach makes the production path honest immediately. There is no second, hidden schema path that can drift away from Alembic.

## Alternatives Considered

### Option A: Alembic Required Immediately

Alembic owns all Postgres schema changes now. Store constructors validate, but do not create, schema.

Pros:

- Clear production model.
- No schema drift between startup code and migrations.
- Failed deployments fail early with actionable operator guidance.
- Matches the user's instruction: no fallback, it needs to work.

Cons:

- Local Postgres users must run migrations before starting the app.
- Tests that instantiate Postgres stores need a migrated database or mocks.

This is the selected option.

### Option B: Temporary Runtime Fallback

Add Alembic, but leave store constructors creating missing tables for one migration cycle.

Pros:

- Slightly easier local transition.
- Lower risk of breaking ad hoc local Postgres databases.

Cons:

- Keeps two schema authorities.
- Can hide missed migrations.
- Does not match the requested "no fallback" direction.

This option is rejected.

### Option C: Full Persistence Overhaul

Add Alembic and also move the control plane to Postgres, enable RLS, add migration CI, and wire deployment release manifests in one slice.

Pros:

- More complete production platform.

Cons:

- Too much blast radius for one migration foundation.
- RLS role split and control-plane storage need their own designs.
- Harder to verify safely.

This option is deferred.

## Database Scope

The baseline migration owns the current Postgres-backed app schema:

- `chunks`
- `users`
- `conversations`
- `messages`
- `service_keys`
- `platform_accounts`
- `platform_spaces`
- `platform_app_installations`
- `platform_audit_events`
- `intake_records`

The baseline migration also owns:

- `CREATE EXTENSION IF NOT EXISTS vector`
- current foreign keys
- current indexes
- current column defaults
- current JSONB columns

The baseline migration does not create control-plane tables because the app has no Postgres control-plane implementation yet.

## Embedding Dimension

The `chunks.embedding` column requires a fixed pgvector dimension. Today that dimension comes from the configured embedder at app startup.

Alembic needs the dimension before app startup, so the migration path will use an explicit migration setting:

- preferred: `ONEBRAIN_MIGRATION_EMBEDDING_DIM`
- fallback for operator convenience: `ONEBRAIN_EMBEDDING_DIM`
- default: `256`, matching the current local hashing embedder

The baseline migration creates:

```sql
embedding vector(<dimension>)
```

The store-level dimension mismatch guard remains, but it becomes a validation guard only. If the migrated database has a different dimension than the configured embedder, the app raises a clear runtime error and does not mutate schema.

Changing embedding dimension later requires a separate re-embedding migration or background job. The app must never drop or rewrite `chunks` automatically to fix a dimension mismatch.

## Runtime Store Changes

Postgres stores should keep connection behavior and row mapping, but stop creating schema.

Expected constructor behavior:

1. Connect to the database.
2. Verify Alembic has applied the required baseline revision.
3. Optionally verify required tables/columns for clearer error messages.
4. For pgvector, register vector support and verify embedding dimension.
5. Raise a `RuntimeError` with operator guidance when validation fails.

The error should be direct, for example:

```text
Postgres schema is not migrated. Run `alembic upgrade head` with ONEBRAIN_DATABASE_URL before starting OneBrain.
```

The app should not attempt to self-heal schema from store constructors.

## Alembic Configuration

Alembic should read the database URL from OneBrain settings or environment:

- `ONEBRAIN_DATABASE_URL` is the primary source.
- A direct Alembic `sqlalchemy.url` value can remain empty or local-only.

Because the app currently uses `psycopg` directly rather than SQLAlchemy models, migrations can use Alembic operations and raw SQL. SQLAlchemy ORM models are not required for this slice.

The migration environment should:

- fail clearly if no database URL is configured for online migration
- support offline SQL rendering when practical
- keep migration scripts readable and explicit

## RLS Position

Do not enable RLS in the baseline migration.

Reason: `scripts/enable_rls.sql` correctly notes that safe RLS requires a separate migration/owner role and a restricted app role with `app.tenant_id` set per request or transaction. That work is security-sensitive and should be a separate role-splitting slice.

The baseline migration may leave `scripts/enable_rls.sql` in place and update migration docs to describe it as a future hardening migration, not default schema.

## Control Plane Position

Do not create control-plane tables in this baseline.

Reason: the active control-plane store is JSON-backed. Creating unused control-plane tables would give the impression that operator release, backup, health, and rollout records are persisted in Postgres when they are not.

A later slice should add a real `PostgresControlPlaneStore`, tests, factory selection, migration tables, and data migration from JSON if needed.

## Data Flow

Local Postgres setup:

1. Set `ONEBRAIN_VECTOR_STORE=pgvector`.
2. Set `ONEBRAIN_DATABASE_URL`.
3. Set `ONEBRAIN_MIGRATION_EMBEDDING_DIM` if not using the default dimension.
4. Run `alembic upgrade head`.
5. Start FastAPI.

Production deployment:

1. Backup database.
2. Run `alembic upgrade head` using the migration/owner database role.
3. Start or roll the FastAPI app using the app role.
4. Let the operator dashboard release manifest track migration range in a later slice.

Application startup/use:

1. Factory selects memory stores for memory mode, unchanged.
2. Factory selects Postgres stores for `pgvector` mode.
3. Postgres stores validate migrated schema.
4. If validation passes, normal reads/writes continue.
5. If validation fails, the app raises an actionable error.

## Error Handling

Expected failure cases:

- Missing database URL for Alembic online migration:
  - fail before migration begins.
- Database does not have Alembic version table:
  - Postgres stores fail with "run alembic upgrade head".
- Database is behind required revision:
  - Postgres stores fail with current revision and required revision.
- `vector` extension missing:
  - migration should install it; validation can report it if missing.
- `chunks.embedding` dimension mismatch:
  - store raises the existing style of runtime error and refuses to mutate schema.
- Existing database has tables but no Alembic stamp:
  - operator must either migrate/stamp intentionally or use a fresh database. Do not auto-stamp from app code.

## Testing Plan

Automated checks:

- Unit test that Alembic baseline includes the known table names and revision metadata.
- Unit test that schema validation fails when Alembic version is missing or too old.
- Unit test that pgvector dimension mismatch still refuses to mutate schema.
- Existing Python pytest suite.

Useful manual/runtime checks:

- Run `alembic upgrade head` against a local Postgres database with pgvector available.
- Start FastAPI in `pgvector` mode after migration.
- Confirm user, document, conversation, platform, service-key, and intake workflows still operate.
- Start FastAPI against an unmigrated empty Postgres database and verify the error is clear.

CI should at least run the migration module import and offline SQL/build checks. A full Postgres/pgvector integration job can be added later if CI does not already provide a database service.

## Acceptance Criteria

- Alembic is present in dependencies.
- The repo has an Alembic environment and a baseline migration.
- The baseline migration creates the current Postgres-backed schema.
- Postgres store constructors no longer create or alter schema.
- Postgres mode fails clearly if migrations were not applied.
- pgvector dimension mismatch protection remains.
- Documentation explains local and production migration commands.
- Existing tests pass.

## Future Work

- Add a Postgres control-plane store and migrations for deployments, releases, backups, health checks, and rollouts.
- Add a role-splitting migration and enable RLS as a separate hardening slice.
- Add migration dry-run checks in CI with a real Postgres/pgvector service.
- Add backup-before-migration enforcement to release/deploy automation.
- Add release manifest migration ranges to operator workflows.
- Add background workers for ingestion, retrieval, and re-embedding jobs.
