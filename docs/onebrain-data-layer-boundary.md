# OneBrain Data-Layer Boundary

OneBrain is the shared durable intelligence and data layer for connected apps.
It is not a replacement for every app-owned operational table.

## OneBrain Owns

- Durable business memory and reusable knowledge.
- Structured app records that should be learned from, retrieved, audited, or
  shared across apps.
- Embeddings and permissioned retrieval.
- Account, space, app-installation, purpose, and service-key scope.
- Provenance, consent, privacy, audit, retention, and credential metadata.
- Assistant records and communication/customer-service intake records.

## Apps Own

- UI state and product-specific workflows.
- Jobs, queues, leases, locks, retries, outbox rows, idempotency keys, and
  delivery attempts.
- Channel/provider mechanics such as webhook cursors, subscriptions, local
  rate-limit state, and temporary caches.
- Billing, handoff workflow, contact routing, and other app-specific execution
  state unless those facts must become durable memory.

## Required Flow

Connected apps do the operational work, then send durable facts and important
events to OneBrain through scoped service APIs:

```text
app event -> app validation/policy -> OneBrain service API -> durable record,
audit, provenance, retention metadata, and retrieval index
```

Browser clients must never receive a OneBrain service key. Browser surfaces call
their app backend, and the app backend calls OneBrain.

## Failure Policy

- Durable OneBrain writes must fail closed or queue for retry. They must not be
  silently dropped.
- Privacy, audit, consent, credential metadata, and security records are
  OneBrain-required.
- User-facing answer reads may fall back only when the app explicitly marks the
  result as degraded or fallback-sourced.
- Production-like OneBrain deployments must run Postgres/pgvector with enforced
  RLS.

