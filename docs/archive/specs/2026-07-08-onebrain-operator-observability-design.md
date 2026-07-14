# OneBrain Operator Observability Design

## Summary

Add a read-only operator observability snapshot for OneBrain. The snapshot gives admins a production-safe view of queue health, recent failures, ingestion volume, data volume, service-key posture, retrieval configuration, and runtime storage mode.

This keeps OneBrain focused on the data layer. The endpoint reports facts about the brain and its durable work queues; it does not add assistant workflows, reminders, task execution, or cross-project functionality.

## Goals

- Add one admin-only operator endpoint for a complete operational snapshot.
- Expose job queue depth by status and type.
- Expose recent failed jobs with safe metadata and no raw payload or file content.
- Expose ingestion/intake/vector counts.
- Expose service-key counts by active and revoked status.
- Expose retrieval configuration needed to understand answer quality:
  - vector store mode
  - embeddings provider
  - LLM provider
  - retrieval top-k
  - retrieval minimum score
- Keep the implementation testable in memory mode and usable in Postgres mode.
- Reuse existing stores and contracts before adding any new infrastructure.

## Non-Goals

- Do not add personal-assistant behavior.
- Do not add task scheduling, reminders, action execution, or notification logic.
- Do not add a separate metrics database.
- Do not add Prometheus, OpenTelemetry, Redis, Celery, or another service in this slice.
- Do not expose raw service-key secrets, raw job payloads, uploaded file bytes, document text, or intake content.
- Do not implement retention/archive policies in this slice.
- Do not implement service-key rotation in this slice.
- Do not build a large Next.js dashboard in the first backend slice.

## Selected Approach

Add store-level summary methods and an admin-only `GET /api/operator/observability` endpoint.

Pros:

- Keeps operational facts close to the data stores that own them.
- Avoids router-level SQL duplication.
- Works for both memory and Postgres backends.
- Gives the Next.js operator console a stable backend contract later.
- Keeps the first implementation small and shippable.

Trade-offs:

- It is an internal admin snapshot, not a full time-series observability platform.
- Historical charts still need a later metrics/tracing design.
- Each store needs one small read-only summary addition.

This is the selected approach because OneBrain already has the right primitives. The gap is a safe, consolidated read model for operators.

## Alternatives Considered

### Router Queries Stores Directly

Pros:

- Fastest initial implementation.
- No protocol additions.

Cons:

- Would require private-field reads in memory stores or SQL duplication for Postgres.
- Makes the router responsible for storage details.
- Harder to test consistently across backends.

Rejected.

### External Metrics Stack Now

Pros:

- Better charts, alerting, and historical analysis.
- Standard production observability pattern.

Cons:

- Adds new infrastructure before the OneBrain control plane has a stable snapshot contract.
- Does not solve admin UI needs by itself.
- More deployment work for each dedicated customer stack.

Rejected for this slice.

### Build Next.js UI First

Pros:

- Immediate visible dashboard work.

Cons:

- The UI would still need to derive data from many endpoints.
- Job and key summaries are not available in a clean backend contract yet.
- Risks baking frontend assumptions into the data model.

Rejected for the first slice. A UI can be added after the API is stable.

## API Contract

Add:

```text
GET /api/operator/observability
```

Authorization:

- Requires a human admin principal.
- Service keys cannot call this endpoint.
- Non-admin users receive `403`.

Response shape:

```json
{
  "generated_at": "2026-07-08T12:00:00+00:00",
  "runtime": {
    "vector_store": "pgvector",
    "llm_provider": "litellm",
    "embeddings_provider": "litellm",
    "async_ingestion": true
  },
  "retrieval": {
    "top_k": 8,
    "min_score": 0.05
  },
  "storage": {
    "chunks": 123,
    "intake_records": 45
  },
  "service_keys": {
    "total": 4,
    "active": 3,
    "revoked": 1
  },
  "jobs": {
    "total": 20,
    "by_status": {
      "queued": 2,
      "running": 1,
      "retrying": 0,
      "succeeded": 16,
      "failed": 1
    },
    "by_type": {
      "document_ingest": 10,
      "service_capture": 5,
      "service_intake": 5
    },
    "recent_failures": [
      {
        "id": "job_abc",
        "type": "document_ingest",
        "tenant_id": "acme",
        "account_id": "acme",
        "space_id": "sp_service",
        "attempts": 3,
        "max_attempts": 3,
        "error": "embedding provider timeout",
        "created_at": "2026-07-08T11:45:00+00:00",
        "updated_at": "2026-07-08T11:47:00+00:00",
        "completed_at": "2026-07-08T11:47:00+00:00"
      }
    ]
  }
}
```

The response may include zero counts when a store is empty. It should not fail just because there are no jobs, no keys, or no intake records.

## Store Contracts

Extend the job store with a read-only summary method:

```python
def summary(self, recent_failures_limit: int = 10) -> JobSummary: ...
```

`JobSummary` should include:

- total count
- counts by status
- counts by type
- recent failed jobs, newest first

Extend the service-key store with a read-only summary method:

```python
def summary(self, tenant_id: str = "") -> ServiceKeySummary: ...
```

For operator observability, an empty `tenant_id` means all tenants. Existing tenant-scoped key list endpoints remain tenant-scoped.

The vector store and intake store already expose `count()`, so no new methods are needed for the first storage counts.

## Data Flow

1. Admin calls `GET /api/operator/observability`.
2. The router validates admin access using the existing operator admin helper.
3. The router reads current settings for runtime and retrieval configuration.
4. The router reads vector chunk count from the vector store.
5. The router reads intake record count from the intake store.
6. The router reads service-key summary from the service-key store.
7. The router reads job summary from the job store.
8. The router returns a sanitized snapshot.

No customer content, uploaded file bytes, raw job payloads, or service-key secrets are returned.

## Error Handling

- Non-admin principals receive `403`.
- If a required store raises a schema validation error in Postgres mode, the endpoint should return the existing FastAPI error behavior rather than masking migration problems.
- Empty stores return zero counts and empty lists.
- Recent failure errors are truncated to a safe display length.
- The endpoint should not include job payloads or result objects because they may contain integration-specific metadata.

## Testing Plan

Automated tests:

- Memory job store summary returns total, by-status, by-type, and recent failures.
- Memory service-key store summary returns active and revoked counts.
- Operator observability endpoint requires admin.
- Operator observability endpoint aggregates settings, chunks, intake records, service keys, and jobs.
- Recent failed jobs do not expose payload, result, file bytes, or service-key secrets.
- Existing Python suite remains green.

Postgres coverage:

- Add Postgres summary SQL behind the same store interfaces.
- Keep tests focused on method behavior where fake connection tests are practical; rely on existing schema validation tests for table presence.

## Acceptance Criteria

- `GET /api/operator/observability` exists and is admin-only.
- The endpoint returns runtime, retrieval, storage, service-key, and job summary sections.
- Job queue depth is visible by status and type.
- Recent failed jobs are visible without raw payloads or content.
- Service-key counts include active and revoked keys without exposing secrets.
- The implementation works in memory mode and Postgres mode.
- Tests cover the new store summaries and endpoint behavior.

## Follow-Up Work

- Add a compact Next.js operator observability panel using this endpoint.
- Add service-key rotation and usage audit views.
- Add retention/archive policy enforcement and audit views.
- Add retrieval evaluation and tracing records.
- Add historical metrics if OneBrain needs trends and alerting beyond a current snapshot.
