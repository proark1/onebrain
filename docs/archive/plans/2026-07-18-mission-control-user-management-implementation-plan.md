# Mission Control user management implementation plan

**Date:** 2026-07-18

**Design:** `docs/archive/specs/2026-07-18-mission-control-user-management-design.md`

## Objective

Implement deployment-scoped user administration in Mission Control through an
outbound, signed, encrypted, idempotent host-agent channel. Operators can list,
create, reset, disable, enable, and safely anonymize users without exposing a
customer management API or retaining plaintext passwords in MC.

## Delivery constraints

- Work only in the isolated `codex/mc-user-management` worktree.
- Reuse the existing `must_change_password`, password hashing, session store,
  fleet-key authentication, desired-state trust, and append-only audit systems.
- Never place passwords, hashes, directory contents, or raw customer errors in
  heartbeats, logs, URLs, shell arguments, browser storage, or plaintext MC
  persistence.
- Keep customer public routes unchanged; execution is a typed stdin CLI invoked
  by the root-owned host agent.
- Make all mutations replay-safe and transactionally couple the account change,
  session revocation, local audit, and encrypted receipt.

## Implementation order

### 1. Add the persistence contracts and migration

- Add MC job and customer receipt domain types plus memory stores.
- Add PostgreSQL stores with atomic leasing, result completion/consumption,
  expiry, and receipt replay.
- Add the next Alembic migration and required schema validation.
- Test memory/PostgreSQL parity and ensure sensitive columns contain ciphertext
  only.

### 2. Add result cryptography and command envelopes

- Implement canonical `user-management-command.v1` signing/verification with
  explicit domain separation.
- Implement per-command X25519/HKDF/AES-GCM result encryption.
- Seal MC request payloads and result private keys with the existing secret
  cipher.
- Test context binding, tampering, wrong deployment/key, expiry, and atomic
  one-time secret consumption.

### 3. Implement customer account lifecycle transactions

- Add server-authoritative human role/location discovery.
- Add list, create, reset, disable, enable, ownership-check, and anonymizing
  delete operations.
- Enforce normalized unique email, legal state transitions, session revocation,
  last-active-admin protection, and ownership blockers.
- Record an encrypted idempotency receipt in the same transaction as each
  mutation; mirror behavior in memory for tests.

### 4. Add the internal stdin CLI and host-agent poller

- Add a strict JSON stdin/stdout CLI that verifies the signed command before
  dispatching a typed operation.
- Add a root-owned poller with atomic ciphertext-only retry outbox and bounded
  cleanup.
- Wire the poller into the box agent without exposing routes or credentials to
  customer containers.
- Add capability reporting and deployment assets/tests.

### 5. Add fleet agent endpoints and operator APIs

- Add deployment-key endpoints to lease commands and acknowledge encrypted
  results.
- Add operator-admin endpoints for directory refresh, job polling, lifecycle
  mutations, and atomic secret reveal.
- Require recent authentication for mutations and enforce deployment scope and
  capability freshness.
- Audit directory reads and mutations without identity or secret fields.

### 6. Regenerate contracts and build the MC Users interface

- Read and apply the Vercel React best-practices skill before editing React.
- Regenerate OpenAPI and extend TypeScript API types/client functions.
- Add MC-only Users navigation/page and a focused panel for deployment
  selection, directory state, forms, confirmations, progress, typed failures,
  and one-time reveal.
- Ensure deployment switching and dismissal clear secret state from memory.

### 7. Verify end to end

- Run focused auth, sessions, fleet, migration, transport, CLI, and lifecycle
  tests.
- Run frontend unit tests, typecheck, lint, and production build.
- Run OpenAPI drift, migration lint, route-isolation, secret scans, and
  `git diff --check`.
- Exercise a fake-agent lost-response retry through MC -> customer -> MC and
  confirm the mutation occurs once and the password reveals once.

### 8. Ship safely

- Stage only plan and feature files, commit, and push the feature branch.
- Pull `main --ff-only`, merge without conflicts, run a final smoke check, and
  push `main` only if both worktrees are clean and no unrelated state blocks the
  repository shipping workflow.
