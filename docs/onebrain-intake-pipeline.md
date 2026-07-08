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

## First Supported Records

- `message`
- `document`
- `contact`
- `task`
- `fact`
- `policy`
- `note`
- `transcript`

## First Supported Intents

- `question`
- `complaint`
- `booking`
- `sales_lead`
- `task`
- `knowledge_update`
- `internal_note`

The current classifier is deterministic and auditable. A later LLM classifier
can be added behind the same output contract, but the storage and privacy model
should remain stable.
