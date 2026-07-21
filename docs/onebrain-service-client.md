# OneBrain Service Client

External tools should use scoped service keys, never user cookies. A key is
issued per customer account, app, space set, purpose set, and read/write scope.

## Python SDK

```python
from onebrain_sdk import OneBrainClient

client = OneBrainClient(
    "https://onebrain.example",
    "obk_...",
    account_id="acme",
    space_id="sp_acme_customer_service",
    app_id="communication",
)

print(client.capabilities())
print(client.brand_theme())

client.intake(
    "Customer asked whether Friday appointments are available.",
    title="Website chat",
    source="communication",
    record_type="message",
)

client.store_message(
    channel="whatsapp",
    sender="+491234567",
    external_id="wamid.123",
    text="I need help with my booking.",
)

answer = client.ask(
    "What should I tell this customer?",
    purpose="customer_service_answer",
)
```

## Service Surface

- `GET /api/service/capabilities`
- `GET /api/service/brand-theme`
- `PUT /api/service/brand-theme`
- `POST /api/service/intake`
- `POST /api/service/capture`
- `POST /api/service/records/delete`
- `GET /api/service/tombstones`
- `POST /api/service/tombstones/{tombstone_id}/ack`
- `POST /api/service/ask`
- `POST /api/service/kpis/snapshots`

All calls require `Authorization: Bearer <service-key>`.

`/api/service/intake` is the preferred path for new integrations. It normalizes
incoming data into a structured OneBrain record with record type, intent,
classification, confidence, status, summary, safe extracted facts, account,
space, app, and purpose. `/api/service/capture` remains available for raw legacy
capture.

Erasure moves in both directions and always takes the write scope.
`/api/service/records/delete` is module-initiated: the module names a record by
the same `source_ref` it used at intake, and the canonical copy here is erased.
It is idempotent, audited, and refused while the scope is under a legal hold.
`/api/service/tombstones` is the reverse feed: the module polls forward from
`cursor` for erasures raised here, mirrors each one, and acks it.

`/api/service/kpis/snapshots` accepts bounded, transactional batches of
aggregate KPI observations for the key's pinned account and spaces. It requires
a KPI Dashboard key with write scope and the `kpi_snapshot_write` purpose. See
the [KPI Dashboard runbook](kpi-dashboard.md) for the payload, idempotency, and
privacy contract.

The service API is intentionally narrow. It stores data in the scoped OneBrain
account/space and returns public-ceiled answers without sources. The operator
dashboard can inspect metadata, keys, versions, and rollout state, but it does
not expose customer content.

## Brand Theme

Assistant and communication tools can fetch their resolved customer/app theme
with:

```http
GET /api/service/brand-theme
Authorization: Bearer <service-key>
```

The response contains normalized color tokens such as `primary_color`,
`accent_color`, `background_color`, `surface_color`, and `text_color`. Resolution
uses the app override first, then the account default, then the built-in Assad
Dar based OneBrain theme.

Tools with a write-scoped app key can store their own app-level override:

```http
PUT /api/service/brand-theme
Authorization: Bearer <service-key>
Content-Type: application/json

{"primary_color":"#123456","accent_color":"#a66e2f"}
```

The update is limited to the key's pinned account and app.

## Key Lifecycle

Admins can list key metadata with `GET /api/service-keys`. Responses include
safe lifecycle fields such as `last_used_at`, `last_used_endpoint`,
`use_count`, `rotated_from_id`, and `revoked_at`. They never include the key
hash or plaintext secret.

Rotate a key with:

```http
POST /api/service-keys/{key_id}/rotate
```

The response returns the new plaintext once. The old key is revoked immediately,
so update the calling integration before discarding the new value.
