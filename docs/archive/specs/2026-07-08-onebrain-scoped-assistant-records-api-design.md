# OneBrain Scoped Assistant Records API Design

## Goal

Expose assistant memory records as scoped OneBrain data, not assistant workflow
functionality. External assistants can query records they are allowed to see and
decide what to do with them outside OneBrain.

## Design

Add `GET /api/service/assistant/records`.

The endpoint filters stored assistant records from `intake_records` by:

- `record_type`
- `intent`
- `account_id`
- `space_id`
- `purpose`
- `status`
- `limit`

It requires a service key with read scope. The endpoint enforces tenant,
account, space, purpose, and assistant-app constraints. It checks platform app
access before returning records and skips records outside the key's visibility.
Explicitly disallowed filters return `403`.

No reminder, task execution, notification, or scheduling behavior is added.
OneBrain remains the scoped memory/database layer.

## SDK

Add:

- `list_assistant_records(...)`
- `list_memory_records(...)`

Both call the scoped records endpoint and pass account/space defaults from the
client when configured.

## Testing

Add tests for:

- listing task and follow-up records as data,
- refusing a disallowed space filter,
- keeping secret-reference records reference-only,
- SDK query construction.
