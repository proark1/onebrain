# OneBrain Intake Pipeline

The intake pipeline turns every incoming data point into a structured OneBrain
record before it becomes usable knowledge.

## Flow

1. Receive data from an app, employee upload, or service adapter.
2. Resolve account, space, app, and purpose.
3. Check service-key and app-installation access.
4. Classify record type and intent.
5. Scan for PII and assign classification.
6. Extract safe facts such as signals, dates, amounts, and PII counts.
7. Store the original content and structured metadata in the intake store.
8. Include intake records in GDPR export/delete operations.

## Record Types

[`app/intake/base.py`](../app/intake/base.py) holds the authoritative sets. This
doc names a representative few; it does not mirror them.

`RECORD_TYPES` currently has 29 members, including `message`, `document`,
`contact`, `task`, and `transcript`.

## Intents

`INTENTS` currently has 23 members, including `question`, `complaint`,
`booking`, `sales_lead`, and `knowledge_update`.

An explicit `record_type` or `intent` is taken as given only when it is a member
of its set, and is then stored at 0.98 confidence. Any other value falls through
to keyword classification, which defaults to `note` and `internal_note` at low
confidence.

The current classifier is deterministic and auditable. A later LLM classifier
can be added behind the same output contract, but the storage and privacy model
should remain stable.
