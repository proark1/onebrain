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
- `POST /api/service/capture`
- `POST /api/service/ask`

All calls require `Authorization: Bearer <service-key>`.

The service API is intentionally narrow. It stores captured text in the scoped
OneBrain account/space and returns public-ceiled answers without sources. The
operator dashboard can inspect metadata, keys, versions, and rollout state, but
it does not expose customer content.
