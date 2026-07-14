# OneBrain Postgres Worker Jobs Design

## Summary

Add a durable background job system for OneBrain using Postgres as the first queue backend. This moves expensive write-side ingestion work out of request handlers without adding Redis, Celery, or another infrastructure dependency.

The first worker implementation focuses on document ingestion and service-side write flows. Retrieval answers stay synchronous in this slice because the current retrieval path already streams answers and needs a separate trace/evaluation design before it becomes a background workflow.

## Goals

- Add a Postgres-backed job queue owned by Alembic migrations.
- Add a Python job store with enqueue, claim, complete, fail, and retry behavior.
- Add a worker runner command that processes queued jobs.
- Move production Postgres document ingestion behind jobs.
- Move service capture and structured intake behind jobs where async mode is requested or required.
- Add a job status endpoint so clients can poll progress and outcome.
- Preserve local memory-mode ergonomics for tests and demos.
- Keep the queue inspectable and recoverable using normal SQL.

## Non-Goals

- Do not add Redis, Celery, RabbitMQ, or a separate queue service in this slice.
- Do not add a full UI job dashboard in this slice.
- Do not change retrieval answer streaming in this slice.
- Do not implement scheduled retention workers in this slice.
- Do not implement re-embedding workers in this slice.
- Do not add object storage in this slice.
- Do not redesign the ingestion, intake, retrieval, or service-key authorization contracts.

## Current State

The app currently performs write-side work inline:

- `POST /api/upload`
  - reads the upload body
  - extracts text
  - scans for PII
  - chunks text
  - embeds chunks
  - writes chunks to the vector store
  - returns a `DocumentSummary`
- `POST /api/service/capture`
  - checks service-key access
  - runs text ingestion inline
  - returns captured document id and chunk count
- `POST /api/service/intake`
  - checks service-key access
  - classifies and stores structured intake inline
  - returns the intake record

This is acceptable for small demos, but production uploads and service integrations should not hold HTTP requests open while extraction, OCR, embedding, or provider calls run.

Alembic is now mandatory for Postgres schema. That makes Postgres the right place for a first durable queue because the schema is versioned and deployed with the backend.

## Selected Approach

Use a Postgres-backed queue first.

Pros:

- No new infrastructure.
- Jobs survive restarts.
- Operators can inspect and repair jobs with SQL.
- Fits the Alembic migration path.
- Good enough for low-to-medium ingestion volume.

Trade-offs:

- It will not scale like a dedicated queue under very high throughput.
- Workers must use careful row claiming to avoid double-processing.
- Polling and cleanup need explicit policies.

This is the selected option because OneBrain is still consolidating core backend architecture. A dedicated queue can be added later behind the same job-store interface if throughput requires it.

## Alternatives Considered

### Redis And Celery Now

Pros:

- Mature worker ecosystem.
- Better for high-volume async workloads.
- Rich retry and scheduling tools.

Cons:

- Adds another service to every customer deployment.
- Adds operational work before the workload requires it.
- Harder to keep local and dedicated Railway deployments simple.

Rejected for this slice.

### FastAPI BackgroundTasks Only

Pros:

- Very small implementation.
- No schema changes.

Cons:

- Jobs do not survive process restarts.
- No durable status.
- Not suitable for production ingestion.

Rejected for production.

### Keep Everything Synchronous

Pros:

- No new architecture.
- Existing clients keep simple response contracts.

Cons:

- Uploads can time out.
- Request workers are occupied by extraction, OCR, embedding, and provider calls.
- Failed jobs have poor retry/recovery behavior.

Rejected as the production direction.

## Data Model

Add a migration after the Alembic baseline with two tables.

### `jobs`

Columns:

- `id TEXT PRIMARY KEY`
- `type TEXT NOT NULL`
- `status TEXT NOT NULL`
- `tenant_id TEXT NOT NULL`
- `account_id TEXT NOT NULL DEFAULT ''`
- `space_id TEXT NOT NULL DEFAULT ''`
- `requested_by TEXT NOT NULL DEFAULT ''`
- `payload JSONB NOT NULL DEFAULT '{}'`
- `result JSONB`
- `error TEXT NOT NULL DEFAULT ''`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `max_attempts INTEGER NOT NULL DEFAULT 3`
- `run_after TIMESTAMPTZ NOT NULL DEFAULT now()`
- `locked_by TEXT NOT NULL DEFAULT ''`
- `locked_at TIMESTAMPTZ`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `completed_at TIMESTAMPTZ`

Indexes:

- `(status, run_after, created_at)` for worker claiming
- `(tenant_id, account_id, space_id, created_at DESC)` for scoped status listing later
- `(locked_at)` for stale-lock recovery later

Statuses:

- `queued`
- `running`
- `retrying`
- `succeeded`
- `failed`

Types for the first slice:

- `document_ingest`
- `service_capture`
- `service_intake`

### `job_files`

Columns:

- `id TEXT PRIMARY KEY`
- `job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE`
- `filename TEXT NOT NULL`
- `content_type TEXT NOT NULL DEFAULT ''`
- `size_bytes INTEGER NOT NULL`
- `data BYTEA NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

Reason:

The first implementation should be self-contained. Upload bytes are stored in Postgres for the job, capped by `ONEBRAIN_MAX_BODY_BYTES`. Later, object storage can replace `job_files` behind a file-reference abstraction.

## Job Store

Add a small job subsystem under `app/jobs`.

Expected modules:

- `app/jobs/base.py`
  - dataclasses and protocols
- `app/jobs/postgres.py`
  - Postgres job store
- `app/jobs/memory.py`
  - local/test job store
- `app/jobs/factory.py`
  - selects Postgres when `vector_store=pgvector`, memory otherwise
- `app/jobs/handlers.py`
  - job handler dispatch

Core operations:

- `enqueue(type, tenant_id, account_id, space_id, requested_by, payload, file=None)`
- `get(job_id)`
- `claim(worker_id, limit=1)`
- `mark_succeeded(job_id, result)`
- `mark_failed(job_id, error)`
- `mark_retry(job_id, error, run_after)`

Claiming should use a transaction and row lock:

```sql
SELECT id
FROM jobs
WHERE status IN ('queued', 'retrying')
  AND run_after <= now()
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT %s
```

The worker then updates those rows to `running`, increments `attempts`, sets `locked_by`, and sets `locked_at`.

## Worker Runner

Add a command:

```bash
python -m app.workers.run
```

Expected behavior:

- Creates a worker id at startup.
- Polls for due jobs.
- Claims a small batch.
- Dispatches each job by type.
- Marks jobs succeeded, failed, or retrying.
- Uses exponential-ish retry delay for retryable failures.
- Logs job id, type, tenant, status, duration, and error summary.
- Handles `Ctrl+C` gracefully after the current job.

Config additions:

- `ONEBRAIN_WORKER_POLL_SECONDS`, default `2`
- `ONEBRAIN_WORKER_BATCH_SIZE`, default `1`
- `ONEBRAIN_JOB_MAX_ATTEMPTS`, default `3`
- `ONEBRAIN_ASYNC_INGESTION`, default `true` when `vector_store=pgvector`, `false` for memory mode

## API Surface

Add job status:

- `GET /api/jobs/{job_id}`

Response fields:

- `id`
- `type`
- `status`
- `tenant_id`
- `account_id`
- `space_id`
- `result`
- `error`
- `attempts`
- `created_at`
- `updated_at`
- `completed_at`

Authorization:

- Human users can read jobs in their own tenant.
- If a job is account/space scoped, the status read should pass the same space-scope check used by document APIs.
- Service principals can read only jobs they created, or jobs in their pinned account/app scope.
- Error text should be useful but must not include raw file content, secrets, or provider tokens.

## Endpoint Behavior

### Upload

For Postgres/async mode:

1. Existing auth and space-scope checks run in the request.
2. File bytes are read with the existing size cap.
3. A `document_ingest` job is created with upload metadata and file bytes.
4. The endpoint returns `202 Accepted` with job status, including `job_id`.

For memory/sync mode:

1. Keep the existing synchronous response to avoid making local demos harder.
2. Tests can still exercise the pipeline directly.

### Service Capture

For async mode:

1. Existing service-key scope, rate-limit, and platform checks run in the request.
2. A `service_capture` job is created with sanitized payload.
3. The endpoint returns `202 Accepted` with `job_id`.

For sync mode:

1. Keep the current behavior.

### Service Intake

For async mode:

1. Existing service-key scope, rate-limit, and intake routing checks run in the request.
2. A `service_intake` job is created with sanitized payload.
3. The endpoint returns `202 Accepted` with `job_id`.

For sync mode:

1. Keep the current behavior.

## Job Handlers

### `document_ingest`

Input:

- filename
- content type
- classification
- location
- category
- uploaded_by
- tenant_id
- account_id
- space_id
- settings-derived approval/PII flags captured at enqueue time
- file reference

Handler:

1. Load file bytes.
2. Run `IngestPipeline.ingest_file`.
3. Store chunks.
4. Return the same document summary shape currently returned by upload.

### `service_capture`

Input:

- title
- text
- tenant_id
- account_id
- space_id
- uploaded_by service principal id
- approval/PII flags captured at enqueue time

Handler:

1. Run `IngestPipeline.ingest_text` with the same clamped labels as today.
2. Return captured document id and chunk count.

### `service_intake`

Input:

- fields required by `IntakeInput`
- metadata

Handler:

1. Run `IntakePipeline.ingest`.
2. Return the existing intake record output shape.

## Error Handling

- Validation/auth errors that can be known before enqueue stay synchronous and return `4xx`.
- Handler failures are stored on the job.
- Retryable failures move the job to `retrying` until `max_attempts`.
- Exhausted failures move to `failed`.
- Permanent validation failures inside handlers move directly to `failed`.
- Job status responses never include raw uploaded file bytes.
- Worker logs must not include raw content.

## Testing Plan

Automated tests:

- Memory job store enqueue/get/claim/succeed/fail.
- Postgres job store SQL shape or fake-connection behavior where practical.
- Worker dispatch succeeds for `service_intake`.
- Worker dispatch succeeds for `service_capture`.
- Worker dispatch succeeds for `document_ingest` using the local embedder and memory store.
- Worker marks retry/failed on handler exceptions.
- Upload endpoint returns synchronous result in memory mode.
- Async upload path returns a job id when async ingestion is enabled.
- Job status endpoint enforces tenant/scope access.
- Existing Python suite remains green.

Manual/runtime checks:

- Start FastAPI in Postgres mode after migrations.
- Run `alembic upgrade head`.
- Start `python -m app.workers.run`.
- Upload a file and poll `/api/jobs/{job_id}` until succeeded.
- Confirm the document appears in document listing after the job succeeds.
- Stop the worker, enqueue a job, restart the worker, confirm it is processed.

## Acceptance Criteria

- A new Alembic migration adds `jobs` and `job_files`.
- The app has a job-store abstraction and Postgres implementation.
- A worker command can process queued ingestion jobs.
- Production Postgres async ingestion can return `202` with a job id.
- Job status can be fetched through an authorized API endpoint.
- Handler results preserve the current upload/capture/intake result shapes.
- Failed jobs record useful error state without leaking raw content.
- Existing synchronous local demo behavior remains usable.
- Tests cover queue lifecycle and handler dispatch.

## Future Work

- Add a Next.js job status surface for uploads.
- Add worker health and queue-depth metrics to the operator dashboard.
- Add stale-lock recovery.
- Add scheduled retention workers.
- Add re-embedding workers for embedding-model changes.
- Add object storage for large upload payloads.
- Add async retrieval/evaluation jobs and retrieval traces.
- Add Redis/Celery backend if queue volume outgrows Postgres.
