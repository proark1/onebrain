# OneBrain deletion, retention & tombstone contract

```
status:   draft v1 (implementable spec)
date:     2026-07-11
owner:    OneBrain (canonical deletion authority)
supports: target-architecture.md §"deletion/retention" — this is its concrete design
scope:    onebrain + assaddar-ai-communication + personalasisstant
```

This spec turns the architecture's one-paragraph "tombstone contract" into something buildable. It is grounded in the code as it exists on 2026-07-11, names the gaps by file, and sequences the work so the two live data-loss/compliance bugs are fixed first, before any new machinery.

---

## 1. Current state (verified against code)

Nothing the architecture calls "outbox machinery" exists yet. What exists:

**OneBrain**
- Scoped `DELETE … WHERE (tenant_id, account_id, space_id)` primitives in all four stores: `app/store/pgvector.py:235` (chunks — also the search index, colocated), `app/conversations/postgres.py:196`, `app/intake/postgres.py:109`, `app/platform/postgres.py:681` (governance).
- `erase_account_data` (`app/routers/privacy.py:174-213`) runs those four deletes **unconditionally** after an admin + `confirm_account_id` check. It never consults retention or any hold. Audit is preserved (see below) and a `privacy.erased` event is written after.
- `run_retention` (`app/retention/service.py:9-67`) **ignores `duration_days` entirely** — the mere presence of an active policy deletes the *whole* `(account_id, space_id)` scope, not records older than N days. It never checks a hold and never writes `retention_runs`. **This is a data-loss bug, not just an omission.**
- `retention_runs` table (migration 0007) and the `retention_run` job type (`app/jobs/base.py:13`, handler `app/jobs/handlers.py:126`) exist but are **dead** — nothing enqueues the job and nothing writes the table.
- Append-only audit (migration 0010): `platform_audit_events` rejects UPDATE/DELETE/TRUNCATE at the DB. Erasure deletes the described content and leaves the audit row — **by design**. This is the one store that survives an account erase.
- No `legal_hold` anywhere (grep: zero hits). No object storage (documents' original bytes are not persisted past ingest). Module→OneBrain sync is **one-directional** (modules push via intake jobs); OneBrain emits nothing.

**AI-Communication**
- `deleteTenantData` (`packages/db/src/repository.ts:6314`) is a single `DELETE FROM tenants` — completeness = the FK cascade graph.
- Two `ON DELETE SET NULL` FKs leave **orphaned PII** on tenant erasure: `channel_webhook_events.tenant_id` (`schema.ts:494` — the raw inbound Meta payloads: phone numbers, names, message bodies) and `stripe_webhook_events.tenant_id` (`schema.ts:379`). Once orphaned (`tenant_id = NULL`) they are also unreachable by retention, which filters by tenant. (`audit_logs` SET-NULL is deliberate — migration 0023.)
- Export (`exportTenantData`, `repository.ts:6221`) still omits `portal_link_projections`, `answer_feedback`, all billing tables, `onebrain_sync_records`, and `conversation_contacts`.
- Retention (`deleteTenantDataOlderThanRetention`, `repository.ts:6487`) covers conversations + calls + webhook events only; misses contacts, deliveries, handoffs, usage, portal links, billing. No hold.
- The OneBrain client (`packages/core/src/onebrain.ts:207`) exposes only `capabilities/intake/ask` — **no delete/tombstone/export**. Knowledge pushed to OneBrain has no code path to be remotely erased.
- `onebrain_sync_records` (migration 0017) holds `external_record_id` — the remote OneBrain id you'd need to tombstone — but it **CASCADE-deletes with the tenant**. So `deleteTenantData` destroys the pointer before anything can use it.

---

## 2. Principles

1. **OneBrain is the deletion authority.** It records the intent (a tombstone), fans it out to consumers, and only marks the deletion complete when every *required* consumer confirms — the mirror of the write rule ("not synchronized until OneBrain confirms").
2. **Precedence is fixed and total:** `legal hold > erasure > retention expiry`. A held record is never deleted by anything. Erasure overrides retention age. Retention deletes only what is both aged-out and not held.
3. **Tombstones and audit carry no PII.** A tombstone names *what scope* was deleted and *why*, by reference — never the content. Audit rows survive erasure and are content-free.
4. **Idempotent everywhere.** Applying or acking a tombstone twice is a no-op. Consumers may crash and retry.
5. **No silent completion.** A consumer that misses its deadline escalates (alert), it does not get skipped.

---

## 3. Data model (new)

**LegalHold** (OneBrain `app/platform/base.py`, new table `platform_legal_holds`)
```
id, account_id, space_id (""=account-wide), subject_ref (""=whole scope),
reason, legal_basis, created_by, created_at, released_at ("" = active)
```
A `(account, space, subject)` is *held* if any active hold covers it. One helper: `is_held(account_id, space_id, subject_ref) -> bool`.

**Tombstone** (OneBrain, new table `platform_tombstones`)
```
id, account_id, space_id, target_type (account|space|document|conversation|contact|subject),
target_ref, reason (erasure|retention|correction|offboarding), requested_by, requested_at,
status (pending|confirmed|failed|expired), deadline, correlation_id
```
plus a child `platform_tombstone_consumers`:
```
tombstone_id, consumer (chunks|conversations|intake|governance|object_store|search_index|
                        communication|assistant), required (bool),
status (pending|confirmed|failed), confirmed_at, error
```
A tombstone is `confirmed` only when every `required` consumer row is `confirmed`. `object_store`/`search_index` are `required` once those stores exist; a rebuildable index may instead be `delete-or-rebuild` (mark confirmed after a rebuild watermark passes).

**RetentionPolicy** (exists) — start actually consuming `duration_days`; keep `legal_basis`.

---

## 4. The two propagation directions

Deletion crosses the service boundary both ways; the contract must cover both.

### 4a. Module-initiated → OneBrain (the common case)
Communication deletes a contact/tenant; the knowledge it pushed to OneBrain must be erased remotely.

- **OneBrain adds** `POST /api/service/records/delete` (service surface, scoped to the key's account, purpose `gdpr_delete`), body `{ source_ref | external_record_id, reason }`. It deletes the referenced chunks and writes a tombstone + `privacy.erased` audit. Idempotent (unknown ref → 200 no-op).
- **Communication adds** `OneBrainServiceClient.delete(ref)` → that endpoint, and a `BrainProvider.delete` method.
- **The ordering bug is the hard part.** `onebrain_sync_records` (holding `external_record_id`) CASCADE-deletes with the tenant, so a naive "delete tenant, then tell OneBrain" loses the refs. Two acceptable fixes — pick one:
  - **(preferred) Local outbox:** before `deleteTenantData` runs, in the *same transaction* insert one `onebrain_delete_outbox` row per `external_record_id` in scope (a table with **no tenant FK**, or `tenant_id` nullable + no cascade). A worker drains the outbox, calls `OneBrainServiceClient.delete`, and marks each row done on OneBrain's confirmation. The write is not complete until OneBrain confirms — same durability rule as sync.
  - **(alt) Surviving tombstone rows:** change `onebrain_sync_records.tenant_id` to `SET NULL` + a `status='pending_deletion'` state, exactly the pattern migration 0023 used for `audit_logs`, so the row (and its `external_record_id`) survives the cascade for the worker to act on, then is removed on confirmation.

### 4b. OneBrain-initiated → modules (offboarding, subject erasure decided centrally)
OneBrain decides an account/subject must be erased everywhere.

- OneBrain writes a tombstone with `communication`/`assistant` as required consumers.
- Sync today is one-directional (modules poll). Reuse that: **OneBrain adds** `GET /api/service/tombstones?since=<cursor>` (scoped to the key's account) and `POST /api/service/tombstones/{id}/ack`. The module's existing sync worker polls the feed, applies the local deletion, and acks. No new push infrastructure; it rides the pull loop that already exists (`apps/workers/src/onebrain-sync.ts`).
- A tombstone whose required consumers haven't all acked by `deadline` flips to `expired` and raises an alert in the metadata-only control plane — never silently `confirmed`.

---

## 5. Fixes to land first (self-contained, high-value, no new machinery)

These fix live bugs and are independently shippable before any tombstone/outbox work.

**OneBrain**
1. **`run_retention` age filter** (`app/retention/service.py`): delete only records older than `policy.duration_days`; skip anything under an active legal hold; **write a `retention_runs` row** (dry-run count + result). This alone stops the current whole-scope deletion.
2. **`erase_account_data` hold check** (`app/routers/privacy.py`): if any active hold covers the scope, refuse with a clear, audited `legal_hold_blocks_erasure` response instead of deleting. (Erasure still overrides retention *age* — only hold blocks it.)
3. **LegalHold model + admin endpoints** (create/release/list) + `is_held` helper; wire into 1 and 2.
4. **Enqueue retention** on a schedule (the `retention_run` job type already exists and is unused) so retention actually runs.

**Communication**
5. **Close the orphan gap:** `channel_webhook_events` + `stripe_webhook_events` → either `ON DELETE CASCADE` or explicit delete inside `deleteTenantData`, **plus a one-time sweep of already-orphaned `tenant_id IS NULL` rows.**
6. **Extend export** to `portal_link_projections`, `answer_feedback`, billing, `onebrain_sync_records`, `conversation_contacts`.
7. **Broaden retention** beyond conversations/calls/webhooks; add the hold check.

---

## 6. Rollout sequence

1. **Phase 1 — bug fixes (§5.1-5.2, 5.5-5.7).** No new cross-service contract; fixes data-loss + erasure-completeness now. Fully unit-testable in each repo.
2. **Phase 2 — legal hold (§3, §5.3-5.4).** Model + precedence wired into retention and erase in both repos.
3. **Phase 3 — module→OneBrain erasure (§4a).** OneBrain delete endpoint + client method + the outbox that captures `external_record_id` before cascade. This is the piece that makes remote erasure real.
4. **Phase 4 — OneBrain→module tombstone feed (§4b)** + confirmation/deadline/escalation. Needed for central offboarding and subject-wide erasure.
5. **Phase 5 — conformance:** an erasure canary (create → sync to OneBrain → delete → assert gone in *both* stores, audit preserved, tombstone `confirmed`), a legal-hold-blocks-erasure test, and a retention-respects-age-and-hold test, run in both repos' CI.

Object storage is not built yet; when it lands it becomes a required tombstone consumer (§3) with `{account}/{space}/{id}` keys so a scope delete is a prefix delete. The contract reserves that consumer slot now so it isn't retrofitted later.

---

## 7. Acceptance criteria

- A record erased in OneBrain (or via a module delete) is gone from chunks/conversations/intake/governance **and** its remote/module copy, with the audit row preserved and the tombstone marked `confirmed`.
- Retention deletes only records past `duration_days`, writes a `retention_runs` row each pass, and never touches held data.
- An active legal hold blocks both retention and erasure, with an audited refusal.
- A tenant deletion in Communication leaves **no** orphaned `channel_webhook_events`/`stripe_webhook_events` rows and propagates a delete for every `external_record_id` it had synced.
- No deletion is reported complete until every required consumer confirms; a missed deadline alerts rather than silently completing.
