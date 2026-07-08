# OneBrain Service-Key Lifecycle Design

## Summary

Harden service keys as OneBrain's non-human access boundary. The slice adds lifecycle metadata, safe usage tracking, immediate rotation, and audit events for key management actions.

This remains OneBrain data-layer work. It does not add assistant workflows, communications behavior, scheduling, notifications, or external app functionality. Separate projects keep their behavior; OneBrain strengthens the credentials they use to access scoped data.

## Goals

- Track safe service-key lifecycle metadata:
  - `last_used_at`
  - `last_used_endpoint`
  - `use_count`
  - `rotated_from_id`
  - `revoked_at`
- Add a Postgres migration for the new columns.
- Keep Postgres startup strict: stores require the new Alembic head.
- Update memory and Postgres stores through one interface.
- Record usage after a key authenticates successfully.
- Never record raw bearer tokens, secrets, request bodies, or customer content.
- Add admin rotation:
  - `POST /api/service-keys/{key_id}/rotate`
  - creates a new key with the same tenant, scopes, account, app, spaces, purposes, and label
  - revokes the old key immediately
  - returns the new plaintext once
- Add audit events for mint, revoke, and rotate.
- Expose lifecycle metadata in service-key list/operator responses without exposing hashes or secrets.

## Non-Goals

- Do not add assistant-side task, reminder, or action behavior.
- Do not build the Next.js key dashboard in this slice.
- Do not add a grace-period overlap for rotated keys.
- Do not support cross-tenant key management.
- Do not expose key hashes, raw secrets, bearer tokens, request bodies, document text, or intake content.
- Do not add external secret-manager integration yet.
- Do not implement automatic key expiry in this slice.
- Do not add IP allowlists or mTLS in this slice.

## Selected Approach

Add lifecycle columns to `service_keys`, update store contracts, and make key usage recording part of successful service-principal resolution.

Pros:

- Strengthens the central auth boundary used by other projects.
- Gives operators enough information to see stale, active, or rotated credentials.
- Keeps implementation inside existing Python/FastAPI and store patterns.
- Uses Alembic, matching the current no-runtime-schema-fallback rule.
- Avoids adding another audit or metrics store.

Trade-offs:

- Auth-time usage writes add a small database write to successful service-key requests.
- Immediate rotation means clients must update credentials deliberately.
- Historical per-request analytics remain limited to aggregate key metadata and platform audit events.

This is the selected approach because service keys are the data-layer contract between OneBrain and separate apps. It improves safety without turning OneBrain into those apps.

## Alternatives Considered

### Usage Tracking Only

Pros:

- Smaller implementation.
- Helps operators find stale keys.

Cons:

- Leaves manual key replacement as the only rotation path.
- Does not close the gap around auditability of key lifecycle actions.

Rejected.

### Rotation With Grace Overlap

Pros:

- Easier rollout for clients that need a short deploy window.

Cons:

- Keeps two valid secrets alive for one integration.
- Requires expiry scheduling or manual cleanup.
- Adds policy decisions before OneBrain has key expiry enforcement.

Rejected for this slice. Immediate rotation is stricter and simpler.

### Full Next.js Dashboard First

Pros:

- More visible operator workflow.

Cons:

- The backend lifecycle contract is not ready.
- UI would duplicate or infer lifecycle state.

Rejected for this slice. UI should come after the API contract is stable.

## Data Model

Add migration:

```text
migrations/versions/0003_service_key_lifecycle.py
```

Revision:

```text
revision = "0003_service_key_lifecycle"
down_revision = "0002_postgres_worker_jobs"
```

Add columns:

```sql
ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS last_used_endpoint TEXT NOT NULL DEFAULT '';
ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS rotated_from_id TEXT NOT NULL DEFAULT '';
ALTER TABLE service_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS service_keys_status_idx ON service_keys (tenant_id, status);
CREATE INDEX IF NOT EXISTS service_keys_last_used_idx ON service_keys (tenant_id, last_used_at);
```

Update `app/db/schema.py`:

- `REQUIRED_ALEMBIC_REVISION = "0003_service_key_lifecycle"`

Update migration docs to mention the lifecycle columns.

## Store Contract

Extend `ServiceKey`:

```python
last_used_at: str = ""
last_used_endpoint: str = ""
use_count: int = 0
rotated_from_id: str = ""
revoked_at: str = ""
```

Extend `ServiceKeyStore`:

```python
def record_usage(self, key_id: str, endpoint: str) -> ServiceKey: ...
def rotate(self, old_key_id: str, new_key: ServiceKey) -> ServiceKey: ...
```

Behavior:

- `record_usage` succeeds only for an existing active key.
- `record_usage` increments `use_count`, sets `last_used_at`, and stores a sanitized endpoint label.
- `rotate` creates the new key and revokes the old key in one store operation.
- `rotate` must preserve old key data except for the new id/hash/creation/lifecycle fields.
- `rotate` sets `rotated_from_id` on the new key.
- `rotate` sets `status="revoked"` and `revoked_at` on the old key.
- If the old key is missing, cross-tenant, or already revoked, the API returns `404` or `409` as appropriate.

Memory mode persists the new fields in `service_keys.json`. Missing fields from older JSON files default safely.

Postgres mode updates all fields through SQL and relies on the migration.

## Authentication Usage Tracking

After `resolve_service_principal` verifies:

- key exists
- key is active
- secret matches

it records usage through:

```python
get_service_key_store().record_usage(key.id, endpoint)
```

The endpoint label should be safe and coarse. Preferred labels:

- `service.capabilities`
- `service.intake`
- `service.capture`
- `service.ask`
- `service.assistant.records.create`
- `service.assistant.records.list`
- `service.assistant.records.read`
- `service.assistant.audit`
- `jobs.read`

Implementation can pass the label from endpoint dependencies or use request route metadata. It must not store raw paths with query strings or request bodies.

If usage recording fails because of storage problems, the request should fail rather than silently losing lifecycle data in Postgres mode. In memory mode, it should behave the same unless the store file is corrupt at startup, which already starts empty.

## API Contract

Update existing key list models:

- `ServiceKeyInfo`
  - add `last_used_at`
  - add `last_used_endpoint`
  - add `use_count`
  - add `rotated_from_id`
  - add `revoked_at`

Add:

```text
POST /api/service-keys/{key_id}/rotate
```

Authorization:

- Human admin only.
- Admin can rotate only keys in their tenant.

Response:

```json
{
  "id": "new_key_id",
  "key": "sk_new_key_id_secret",
  "tenant_id": "acme",
  "scopes": ["read:public"],
  "label": "Communication integration",
  "account_id": "acme",
  "app_id": "communication",
  "space_ids": ["sp_acme_service"],
  "purposes": ["customer_service_answer"],
  "rotated_from_id": "old_key_id"
}
```

The plaintext is returned once, same as key minting.

## Audit Events

Use `platform_audit_events` for account-scoped lifecycle audit.

Mint:

- action: `service_key.minted`
- actor: admin human
- target_type: `service_key`
- target_id: new key id
- account_id: admin tenant
- app_id/purpose/space_id from key metadata when present
- meta:
  - scopes
  - label
  - account_id
  - app_id
  - space_ids
  - purposes

Revoke:

- action: `service_key.revoked`
- target_id: revoked key id
- meta:
  - label
  - app_id
  - space_ids
  - purposes

Rotate:

- action: `service_key.rotated`
- target_id: new key id
- meta:
  - old_key_id
  - new_key_id
  - app_id
  - space_ids
  - purposes

Audit metadata must never include key hashes, raw secrets, or plaintext bearer keys.

## Data Flow

### Successful Service Request

1. Endpoint dependency resolves a service principal from the bearer key.
2. OneBrain parses key id and secret.
3. Store loads key by id.
4. Store verifies key is active and secret matches.
5. Store records usage metadata with a safe endpoint label.
6. Endpoint continues with existing scope, rate-limit, platform access, and data handling.

### Rotate Key

1. Admin calls `POST /api/service-keys/{key_id}/rotate`.
2. API checks admin role.
3. Store loads old key.
4. API confirms old key tenant matches admin tenant and is active.
5. API generates a new id and secret.
6. Store creates the new key and revokes the old key in one operation.
7. API records a `service_key.rotated` audit event.
8. API returns the new plaintext once.

## Error Handling

- Non-admin management requests return `403`.
- Missing or cross-tenant keys return `404`.
- Rotating an already revoked key returns `409`.
- Duplicate generated key id remains an error rather than overwriting.
- Invalid or revoked keys continue to return `401`.
- Usage recording does not run for invalid, revoked, or wrong-secret keys.
- Usage metadata errors in Postgres mode fail the request so operators see the problem.

## Security And Privacy

- No plaintext secret is persisted.
- No key hash is exposed through API responses.
- No request body, query string, customer text, intake content, document text, or uploaded file content is recorded in lifecycle metadata.
- Usage endpoint labels are coarse and controlled by backend code.
- Rotation uses immediate old-key revocation to avoid two valid credentials for the same integration.
- Lifecycle metadata is admin/operator-visible only.

## Testing Plan

Automated tests:

- Migration `0003_service_key_lifecycle` has correct revision/down-revision.
- Schema validation requires the new Alembic head.
- Memory service-key store preserves default lifecycle fields for old records.
- Memory service-key store `record_usage` updates timestamp, endpoint, and count.
- Memory service-key store `rotate` creates a new active key and revokes old key.
- Postgres service-key store SQL includes lifecycle fields and methods.
- `resolve_service_principal` records usage only after a valid secret.
- Invalid, revoked, and wrong-secret keys do not record usage.
- `POST /api/service-keys/{key_id}/rotate` is admin-only and tenant-scoped.
- Rotate response returns new plaintext once and includes `rotated_from_id`.
- Key list responses include lifecycle metadata but never hashes/secrets.
- Mint, revoke, and rotate write audit events without secrets.
- Existing Python suite remains green.

Runtime smoke:

- Apply migrations with `alembic upgrade head`.
- Mint a temporary scoped service key.
- Call `/api/service/capabilities` with the key.
- Confirm `last_used_at`, `last_used_endpoint`, and `use_count` update.
- Rotate the key.
- Confirm old key receives `401` and new key works.
- Confirm account audit includes mint and rotate without plaintext.

## Acceptance Criteria

- Alembic head advances to `0003_service_key_lifecycle`.
- Postgres stores fail clearly until the migration is applied.
- Service keys show lifecycle metadata in admin/operator list responses.
- Successful service-key requests update safe usage metadata.
- Key rotation is available through an admin-only endpoint.
- Rotated old keys stop working immediately.
- Audit events exist for mint, revoke, and rotate.
- No API response, audit record, lifecycle field, or test fixture exposes raw secrets beyond the one-time plaintext mint/rotate response.

## Follow-Up Work

- Add a Next.js key lifecycle panel inside the operator/admin console.
- Add key expiry policy and expiring-soon warnings.
- Add optional grace-period rotation if operational demand justifies it.
- Add IP allowlist or mTLS metadata for high-risk integrations.
- Add historical key usage event table if aggregate metadata is not enough.
