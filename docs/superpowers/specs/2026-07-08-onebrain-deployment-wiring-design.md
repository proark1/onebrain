# OneBrain Deployment Wiring Design

Date: 2026-07-08

## Summary

OneBrain should deploy as three first-class Railway services from the same
repository:

- `onebrain-api`: Python/FastAPI backend.
- `onebrain-admin-ui`: Next.js admin/product UI.
- `onebrain-workers`: Python background worker process.

The Python backend stays the source of truth for auth, access control,
retrieval, ingestion, privacy operations, operator/provisioning flows, service
keys, and database migrations. Next.js stays a server-rendered UI and proxy
layer. Workers process durable Postgres jobs independently from request
handling.

This is a Railway-first design because the existing repo already has Railway
configuration and the unified platform design names Railway as the first
deployment target. The wiring should remain portable through explicit
Dockerfiles and start commands.

## Goals

- Make API, Next.js, and worker deployment repeatable.
- Run Alembic before the API serves traffic when Postgres mode is enabled.
- Make workers wait for the expected migrated schema before processing jobs.
- Keep local memory-mode demos working without requiring Postgres.
- Document the required service commands and environment variables.
- Provide smoke-test commands for a deployed stack.

## Non-Goals

- Do not move the Python backend to TypeScript.
- Do not retire the old FastAPI static frontend in this slice.
- Do not add Redis/Celery; the current Postgres job table remains the queue.
- Do not build a full release orchestrator or rollout-ring automation yet.
- Do not solve production backups or RLS hardening beyond documenting the
  deployment contract.

## Chosen Approach

Use Railway-first multi-service wiring:

1. Build the Python API image from the root `Dockerfile`.
2. Build the worker from the same Python dependency/runtime shape, with a
   worker-specific start command.
3. Build the Next.js UI from `onebrain-web`.
4. Connect the Next.js service to the API through `ONEBRAIN_API_BASE_URL`.

This avoids cross-provider cookie and networking complexity while still making
each process independently restartable and scalable.

## Services

### `onebrain-api`

The API service owns public HTTP traffic to FastAPI.

Start behavior:

1. Inspect runtime settings.
2. If `ONEBRAIN_VECTOR_STORE=pgvector`, run `python -m alembic upgrade head`.
3. Start `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`.

The API Docker image must include:

- `app/`
- `migrations/`
- `alembic.ini`
- `requirements.txt`
- deployment launcher scripts

The service uses `/health` as its Railway health check.

### `onebrain-workers`

The worker service processes durable jobs from Postgres.

Start behavior:

1. Inspect runtime settings.
2. If `ONEBRAIN_VECTOR_STORE=pgvector`, wait until the database has the expected
   Alembic revision.
3. Start `python -m app.workers.run`.

The worker does not expose HTTP traffic and does not need a public domain. It
must share the same Postgres, provider, and app configuration as the API.

### `onebrain-admin-ui`

The Next.js service owns the product/admin UI.

Start behavior:

1. Install dependencies with `npm ci`.
2. Build with `npm run build`.
3. Start with `npm run start` on Railway's injected `PORT`.

The Next.js app calls the Python API through:

```text
ONEBRAIN_API_BASE_URL=https://<api-service-url>
```

For Railway internal networking this can be the private service URL if
available. The browser should continue calling same-origin Next.js routes such
as `/api/onebrain/...`; the Next server proxies those requests to FastAPI.

## Environment Variables

Shared Python API and worker variables:

- `ONEBRAIN_VECTOR_STORE=pgvector`
- `ONEBRAIN_DATABASE_URL`
- `ONEBRAIN_MIGRATION_EMBEDDING_DIM`
- `ONEBRAIN_AUTH_SECRET`
- `ONEBRAIN_COOKIE_SECURE=true`
- `ONEBRAIN_LLM_PROVIDER`
- `ONEBRAIN_EMBEDDINGS_PROVIDER`
- provider API keys, such as `GEMINI_API_KEY`
- `ONEBRAIN_ADMIN_EMAIL`
- `ONEBRAIN_ADMIN_PASSWORD`
- privacy and publication gates such as `ONEBRAIN_PII_PHASE`,
  `ONEBRAIN_REQUIRE_APPROVAL`, and `ONEBRAIN_BLOCK_PUBLIC_ON_PII`

Worker-specific variables:

- `ONEBRAIN_WORKER_POLL_SECONDS`
- `ONEBRAIN_WORKER_BATCH_SIZE`
- `ONEBRAIN_JOB_MAX_ATTEMPTS`

Next.js variables:

- `ONEBRAIN_API_BASE_URL`

Railway injects `PORT` for HTTP services.

## Data Flow

Browser traffic goes to the Next.js service. Next.js renders pages server-side
and uses the existing local proxy route for browser actions:

```text
browser -> onebrain-admin-ui -> /api/onebrain/* -> onebrain-api -> Postgres/provider services
```

Document upload and service intake flows enqueue durable jobs when Postgres mode
uses async ingestion:

```text
browser/app -> onebrain-api -> jobs/job_files tables -> onebrain-workers -> chunks/users/conversations/etc.
```

The API remains the authorization boundary. The worker consumes only jobs that
were created by authorized API flows.

## Migration Behavior

Production Postgres deployments must not rely on runtime table creation. The
API startup launcher runs Alembic before serving requests when Postgres mode is
enabled.

The worker should not start processing until the expected Alembic revision is
visible. If the schema is not ready, the worker waits for a bounded period and
then exits non-zero so the platform can restart it.

Local memory-mode deployments should skip migration automatically so the
existing no-database prototype remains usable.

## Error Handling

- Missing or weak `ONEBRAIN_AUTH_SECRET` still fails API startup.
- Missing `ONEBRAIN_DATABASE_URL` in Postgres mode fails the API/worker startup.
- Failed migrations fail the API startup and prevent traffic.
- Worker schema wait timeout fails the worker startup instead of processing
  against an unknown schema.
- Next.js misconfigured `ONEBRAIN_API_BASE_URL` should surface as failed API
  requests during smoke tests.

## Railway Setup

The implementation should add concrete wiring for:

- root Python Docker image suitable for API and worker runtime assets.
- worker start command or worker Dockerfile.
- `onebrain-web` production Dockerfile or Railway build instructions.
- deployment documentation listing service root, build command, start command,
  and health check for each service.

The existing root `railway.json` may remain API-focused. If Railway cannot
consume multiple service configs from one file, service-specific settings should
be documented explicitly and encoded in Dockerfiles/start scripts where
possible.

## Testing

Implementation checks should include:

- Python tests: `pytest -q`.
- Next.js lint/typecheck/build: `npm run lint`, `npm run typecheck`,
  `npm run build` inside `onebrain-web`.
- Docker syntax/build checks where practical for the Python and Next.js images.
- A deployment smoke-test document covering:
  - API `/health`.
  - Next.js root/session redirect behavior.
  - login with `ONEBRAIN_ADMIN_*`.
  - upload returns a job id in Postgres async mode.
  - worker processes the queued job.

## Implementation Notes

Prefer small deployment launcher scripts over embedding complex shell logic in
Docker `CMD` lines. The launcher scripts can keep migration detection,
schema-wait behavior, and command assembly readable and testable.

The old FastAPI static frontend remains mounted at `/` on the API service until
the Next.js UI fully replaces all needed workflows.

## Approval Criteria

- A new Railway deployment can be created with API, Next.js, Postgres, and
  worker services.
- API boot runs migrations in Postgres mode and serves `/health`.
- Worker boot waits for migrated schema and processes queued jobs.
- Next.js can reach FastAPI through `ONEBRAIN_API_BASE_URL`.
- Local memory-mode Docker/API startup still works without a database.
- Deployment docs explain the exact services, commands, env vars, and smoke
  checks.
