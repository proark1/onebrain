# AI Employees Module Runtime Implementation Plan

**Date:** 2026-07-16

**Status:** Approved design; implementation ready

**Design:** `docs/superpowers/specs/2026-07-16-ai-employees-module-runtime-design.md`

## Objective

Upgrade the existing `ai_employees` module foundation into a persistent,
editable, Gemini-backed AI organization. The module must remain optional,
account/space scoped, auditable, and safe by default. It must support direct
employee conversations, bounded multi-agent missions, internal work products,
approval-bound actions, and a Google Workspace Calendar connector without
turning employee metadata or character prompts into authorization.

The work is delivered as vertical, independently testable slices. Each slice
keeps the existing application runnable and preserves compatibility with the
current provisioning and action-governance contracts.

## Delivery principles

- Tests are written or updated before each behavior change.
- `ai_employees.v2` is the canonical contract; the old import path remains a
  compatibility re-export while callers migrate.
- Memory and Postgres stores expose matching behavior.
- All human endpoints verify session, account, space, installation, membership,
  and purpose before reading or mutating module data.
- All model and connector operations are behind narrow interfaces and can be
  replaced with deterministic fakes in tests.
- The module is useful without Google Calendar and fails closed when Gemini or a
  connector is unavailable.
- No raw provider secret is persisted in module records, prompts, logs, audit
  metadata, or API responses.

## Implementation order

### 1. Establish the v2 module contract and approved roster

**Files**

- Add `app/ai_employees/__init__.py`.
- Add `app/ai_employees/contracts.py`.
- Replace `app/assistant/employees.py` with a compatibility re-export.
- Modify `app/provisioning/bundles.py`.
- Modify `app/platform/base.py` and `app/routers/platform.py` only if the new
  purposes are not derived from the shared contract.
- Rewrite `tests/test_ai_employee_contracts.py`.
- Update `tests/test_provisioning.py`.

**Test first**

- The contract version is `ai_employees.v2`.
- Exactly 16 stable employees exist with the approved names, roles, ages,
  countries, pronouns, traits, reporting lines, and pod assignments.
- The roster contains eight women and eight men, six German, five British, and
  five French characters.
- Clara is the root employee; Oliver reports directly to Clara; the leadership
  council contains Clara, Elodie, Lukas, and Antoine.
- Every standing team is below six employees and mission squads reject a
  seventh participant.
- All seven runtime purposes are provisioned for `onebrain_ai_employees` and
  `full_stack`; unrelated bundles still omit the app.
- Unknown employee IDs, purposes, modes, providers, and role capabilities fail
  closed.

**Implement**

- Define immutable employee, character, organization, model-policy, and action
  contracts in the new module package.
- Include presentation attributes and prompt-safe personality fields, but keep
  permissions and immutable role policy separate.
- Provide pure hierarchy, squad-size, purpose, mode, payload-hash, secret-scan,
  and approval-policy validators.
- Preserve old action-proposal callers through re-exports rather than
  maintaining two rosters.

### 2. Add durable module persistence and migration

**Files**

- Add `migrations/versions/0024_ai_employees_runtime.py`.
- Add `app/ai_employees/base.py`.
- Add `app/ai_employees/memory.py`.
- Add `app/ai_employees/postgres.py`.
- Add `app/ai_employees/factory.py`.
- Modify `app/deps.py`.
- Modify `migrations/versions/0008_rls_hardening.py` only if shared migration
  metadata must enumerate future tenant tables; otherwise keep hardening in
  migration `0024`.
- Add `tests/test_ai_employee_store.py`.
- Extend `tests/test_postgres_schema_validation.py`.

**Test first**

- Installation seeding is idempotent and creates the 16 profiles, published
  default character versions, and Gemini model policies.
- Profiles, versions, policies, conversations, messages, missions,
  participants, runs, memories, connector bindings, and action proposals round
  trip in the memory store.
- The Postgres migration creates all module tables, indexes, foreign keys,
  uniqueness constraints, and forced account/space RLS policies.
- Cross-account and cross-space reads fail in both the store API and Postgres.
- Published versions are immutable; drafts can be replaced only by their human
  author; active runs retain pinned versions.
- Erasure deletes account/space module data while audit/tombstone behavior
  remains governed by the platform contracts.

**Implement**

- Use small immutable dataclasses for records and a store protocol for module
  operations.
- Persist memory-store state under `.onebrain_data` using the repository's
  atomic JSON pattern.
- Store canonical work products in `intake_records` with
  `app_id=ai_employees`; dedicated tables hold operational agent state only.
- Add deterministic IDs or caller-supplied idempotency keys for seed and retry
  operations.

### 3. Add installation, workspace, and administrator authorization guards

**Files**

- Add `app/ai_employees/scope.py`.
- Add `app/routers/ai_employees.py`.
- Modify `app/main.py`.
- Add module schemas to `app/schemas.py` or a focused
  `app/ai_employees/schemas.py`.
- Add `tests/test_ai_employee_api_scope.py`.

**Test first**

- Workspace discovery returns only active `ai_employees` installations in
  spaces visible to the signed-in user.
- Paused or missing installations block new runs and mutations while allowing
  a scoped read-only history response.
- Configure, publish, rollback, connector management, and model-policy changes
  require a human project/account administrator.
- AI/service principals cannot configure characters or approve actions.
- Every endpoint rejects account, space, app, purpose, membership, and
  classification scope mismatches.

**Implement**

- Centralize module-scope resolution so routers and workers use the same
  fail-closed guard.
- Seed defaults lazily and idempotently when an active installation is first
  opened, with an explicit service hook available to provisioning.
- Expose workspace posture and capability flags for the web UI.

### 4. Build the provider-neutral agent backend and prompt compiler

**Files**

- Add `app/ai_employees/backends/base.py`.
- Add `app/ai_employees/backends/litellm.py`.
- Add `app/ai_employees/backends/local.py`.
- Add `app/ai_employees/backends/registry.py`.
- Add `app/ai_employees/prompting.py`.
- Add `app/ai_employees/runtime.py`.
- Modify `app/config.py` with module model, budget, and provider-health settings.
- Add `tests/test_ai_employee_backends.py`.
- Add `tests/test_ai_employee_prompting.py`.

**Test first**

- The normalized stream contract supports text, citations, structured output,
  tool requests, usage, provider session references, warnings, and errors.
- All default employee policies resolve to the configured Gemini model.
- Claude policy can be stored but cannot activate without credentials,
  processing approval, and an isolated technical-agent backend.
- Classification and region policy can reject a configured provider and never
  silently downgrade.
- The prompt stack always orders immutable safety, role policy, published
  character, assignment, scoped memory, evidence, and tool policy.
- Retrieved content, history, and other-agent messages are fenced as untrusted
  context and cannot alter system policy.
- Token, time, and cost budgets fail closed with a sanitized error.

**Implement**

- Adapt LiteLLM/Gemini behind the normalized backend protocol without changing
  the existing generic `/api/ask` path.
- Keep OneBrain as the canonical state owner; provider session IDs are optional
  optimization metadata.
- Add deterministic local/fake backends for tests and local development.

### 5. Deliver persistent direct employee conversations

**Files**

- Extend `app/ai_employees/runtime.py`.
- Extend `app/routers/ai_employees.py`.
- Add `tests/test_ai_employee_conversations.py`.

**Test first**

- A user starts or resumes a conversation by employee ID without supplying the
  character prompt.
- Each turn pins the published character and model-policy versions.
- Retrieval uses only approved evidence visible in the selected account/space.
- Streaming persists the human message, final employee message, citations,
  provider/model, usage, cost, status, and sanitized failure.
- A worker or request retry cannot duplicate the same turn.
- Raw chat does not become approved memory automatically.

**Implement**

- Add conversation create/list/detail and SSE turn endpoints under
  `/api/ai-employees`.
- Compile context from bounded conversation summaries, approved memories, and
  permitted retrieval hits.
- Persist run state before calling a backend and finalize it transactionally or
  idempotently after streaming.

### 6. Deliver versioned character administration

**Files**

- Add `app/ai_employees/characters.py`.
- Extend `app/routers/ai_employees.py`.
- Add `tests/test_ai_employee_characters.py`.

**Test first**

- An administrator can create a draft, preview the effective non-sensitive
  prompt, publish it, roll back, and reset to the default.
- Character drafts enforce size limits, field validation, and recursive secret
  scanning.
- Immutable role, access, approval, provider, and squad policy cannot be
  changed through character fields or prompts.
- A publish creates an immutable version/checksum and affects only new turns or
  missions.
- Concurrent draft/publish attempts detect version conflicts.

**Implement**

- Store editable presentation, personality, voice, working-style, examples, and
  customer prompt fields in versioned character payloads.
- Provide server-generated diffs and a safe preview that excludes hidden system
  policy and secrets.

### 7. Add bounded multi-agent missions

**Files**

- Add `app/ai_employees/missions.py`.
- Extend `app/routers/ai_employees.py`.
- Add `app/workers/ai_employees.py` and register its job type with the existing
  worker dispatcher.
- Add `tests/test_ai_employee_missions.py`.

**Test first**

- Clara selects no more than six participants and one accountable executive.
- A mission executes separate independent positions, one challenge round, one
  accountable plan, and one Clara synthesis in a deterministic state machine.
- Shared evidence is the intersection of participant access; private findings
  can enter only as disclosure-checked summaries.
- Missing Clara or accountable-chief output pauses the mission; optional
  specialist failure marks the result incomplete and blocks consequential
  actions.
- Cancellation, retries, worker restarts, and concurrent workers never advance
  or charge for the same turn twice.
- Turn, token, time, and cost budgets prevent open-ended agent loops.

**Implement**

- Use durable mission/participant/run state and short worker jobs rather than a
  long in-request loop.
- Expose mission create, proposed squad, start, cancel, progress, transcript,
  synthesis, dissent, and action endpoints.

### 8. Add approved memory, internal work products, and governed actions

**Files**

- Add `app/ai_employees/memory_service.py`.
- Add `app/ai_employees/actions.py`.
- Extend `app/routers/ai_employees.py`.
- Extend `app/assistant/contracts.py` only for shared intake vocabulary.
- Add `tests/test_ai_employee_memory.py`.
- Add `tests/test_ai_employee_actions.py`.

**Test first**

- Memory requires provenance, human approval, account/space/classification,
  retention, and author; rejected/deleted memory is never compiled.
- Reports, briefs, plans, tasks, checklists, and proposals are written as scoped
  `intake_records` and remain visible to privacy/export/retention paths.
- Proposal creation verifies source access and computes risk, approver, expiry,
  canonical payload hash, and idempotency server-side.
- Approval requires a fresh human session; payload changes invalidate approval.
- Execution rejects missing/wrong/expired approval, changed grants, revoked
  connectors, changed payloads, duplicate mutations, and prohibited action
  types.
- Audit events reveal decisions and metadata but not raw sensitive content.

**Implement**

- Add memory review/delete and action queue/approve/reject/change-request APIs.
- Keep Tier 0/1 work automatic and Tier 2 writes approval-bound.
- Hard-block Tier 3 autonomous payments, legal signatures, employment,
  privilege, production, destructive security, and privacy operations.

### 9. Add the Google Workspace Calendar connector

**Files**

- Add `app/ai_employees/connectors/base.py`.
- Add `app/ai_employees/connectors/google_calendar.py`.
- Add `app/ai_employees/connectors/secrets.py`.
- Extend `app/config.py`.
- Extend `app/routers/ai_employees.py`.
- Extend `.env.example` without adding real credentials.
- Add `tests/test_ai_employee_google_calendar.py`.

**Test first**

- OAuth start/callback validates signed state, nonce, redirect URI, provider,
  account, space, and initiating human administrator.
- Tokens are held behind an opaque secret reference and never appear in module
  records, prompts, responses, logs, or audits.
- Calendar allowlists and employee capability grants are enforced at proposal
  and execution time.
- A specifically enabled private self-only focus block can execute without a
  second approval.
- Attendees, external invitations, edits/cancellations of other events,
  confidential content, and company commitments require human approval.
- Idempotent retries return the recorded provider event instead of creating a
  duplicate; revoke blocks future work immediately.
- Provider errors are sanitized and transient reads do not cause blind write
  retries.

**Implement**

- Use a narrow HTTP adapter with injectable transport and no Google-specific
  types in the core runtime.
- Store encrypted connector secrets in the deployment secret backend and only
  opaque references in Postgres/module records.
- Expose connection status, scopes, calendars, allowlist, grants, and revoke
  endpoints.

### 10. Replace the static control center with the live module UI

**Files**

- Rewrite `onebrain-web/src/components/ai-employees-panel.tsx`.
- Add focused components under
  `onebrain-web/src/components/ai-employees/`.
- Extend `onebrain-web/src/lib/onebrain-api.ts` and generated
  `onebrain-types.ts`/`openapi.json`.
- Modify `onebrain-web/src/app/globals.css`.
- Add frontend tests if the repository test harness is introduced; otherwise
  cover contract types plus lint, typecheck, production build, and API tests.

**Test first**

- Only active/readable module workspaces are selectable.
- The hierarchy clearly shows Clara, Oliver, the council, and three pods without
  overflow at desktop, tablet, or mobile widths.
- Team, Chats, Missions, Actions, Connections, Models, and Reports render live
  API state.
- Administrators can edit, preview, diff, publish, roll back, reset, pause, and
  select approved model policy.
- Direct chat streams distinct employee responses and citations.
- Mission chat shows distinct speakers, phase, dissent, sources, budgets, and
  incomplete/paused states.
- Approval cards show the exact normalized payload summary/hash, risk, expiry,
  source references, and decision.
- Disabled, paused, unconfigured, degraded, revoked, and healthy states are
  explicit and accessible.

**Implement**

- Keep the visual language of the live KPI and Cockpit surfaces, but use a
  hierarchy-first team view and progressive-detail tabs rather than the current
  static checklist grid.
- Do not embed character or policy data in the browser bundle; fetch it from the
  module API.

### 11. Complete privacy, operations, and release integration

**Files**

- Extend `app/privacy/service.py` and related store contracts.
- Extend module/platform observability and audit summaries.
- Modify deployment/configuration manifests only for documented settings.
- Update `README.md` and module operations documentation.
- Add/extend privacy, deployment, OpenAPI, and smoke tests.

**Test first**

- Export and erasure include all module records, conversations, memories,
  connector metadata, and work products without raw secrets.
- Pausing/uninstalling the module blocks jobs and connector execution while
  preserving read-only history until erasure.
- Metrics aggregate work, cost, failures, approvals, blocks, and stale queues
  without leaking prompt/source content.
- API/worker/admin images boot with the migration and report module health.
- A dummy-data development deployment completes direct Gemini chat, a bounded
  mission, internal finance report, character publish, Calendar connection, and
  approved event smoke tests.

**Implement**

- Add privacy hooks, retention behavior, health details, aggregate reports, and
  operational alerts.
- Document Gemini and Google OAuth secret names, redirect URI, scopes, module
  installation, rollback, and smoke-test procedure.
- Keep Claude inactive until its separate processing and sandbox gate is met.

## Verification sequence

Run focused tests after each slice, then run the complete gate:

```powershell
python -m pytest tests/test_ai_employee_contracts.py tests/test_ai_employee_store.py -q
python -m pytest tests/test_ai_employee_api_scope.py tests/test_ai_employee_backends.py tests/test_ai_employee_prompting.py -q
python -m pytest tests/test_ai_employee_conversations.py tests/test_ai_employee_characters.py tests/test_ai_employee_missions.py -q
python -m pytest tests/test_ai_employee_memory.py tests/test_ai_employee_actions.py tests/test_ai_employee_google_calendar.py -q
python -m pytest -q
python scripts/export_openapi.py onebrain-web/src/lib/openapi.json
npm --prefix onebrain-web run lint
npm --prefix onebrain-web run typecheck
npm --prefix onebrain-web run build
git diff --check
```

Run migration validation against a disposable PostgreSQL test database before
building release images. Do not point tests at a non-test DSN.

## Release and activation

1. Commit task-related files only; preserve unrelated working-tree changes.
2. Push the feature branch and merge it into `main` only after the complete
   local gate passes.
3. Let the repository's release workflow publish immutable API, worker, and
   admin UI images from `main`.
4. Register the candidate and deploy it through the dummy-data development
   gate.
5. Configure Gemini and Google OAuth credentials through deployment secrets,
   never source or database records.
6. Install the `onebrain_ai_employees` bundle, verify all 16 Gemini policies,
   and run the smoke sequence.
7. Treat missing credentials, release variables, development-gate capacity, or
   external OAuth configuration as an activation blocker, not a reason to
   weaken policy or silently change hosting paths.

## Done criteria

- `ai_employees` is one optional, installable, pausable, erasable module.
- The 16-person European organization is the seeded and API-served default.
- Human administrators safely customize and version characters.
- Users hold persistent, scoped Gemini conversations with any enabled employee.
- Clara runs bounded maximum-six missions with separate agent turns and human
  review.
- Agents create governed internal work and approval-bound action proposals.
- Multi-model policy is real and provider-neutral while the initial live roster
  stays Gemini-only.
- Google Calendar is connected through scoped OAuth and idempotent,
  approval-aware writes.
- API, worker, UI, persistence, privacy, deployment, and security checks pass
  before live rollout.
