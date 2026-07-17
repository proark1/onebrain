# Hetzner Production Hardening Implementation Plan

**Date:** 2026-07-17
**Status:** Approved design; implementation ready
**Design:** `docs/superpowers/specs/2026-07-17-hetzner-production-hardening-design.md`

## Objective

Deliver the audited production hardening program without changing live customer infrastructure. The repository will use Hetzner as its sole executable provisioning path, protect multi-replica API operation, recover durable work after failures, and verify release inputs in CI.

The implementation is intentionally additive around persistent data and phased around release risk. Historical `railway_*` columns remain as compatibility storage; live teardown has a disabled-by-default protocol but no delete executor; external credential rotation and production canaries remain operator-owned activation work.

## Cross-cutting rules

- Preserve the unrelated changes to `docs/kpi-dashboard.md` and `.superpowers/` throughout the task.
- Add a focused regression test before every changed security or recovery behavior. Memory and PostgreSQL implementations must expose matching semantics.
- Keep migrations additive, nullable where old rows require compatibility, and ordered after `0024_ai_employees_runtime.py`.
- Fail closed in production; retain explicit development defaults only where a local in-memory implementation is intentional and covered by tests.
- Do not expose provider credentials, raw client IPs, normalized login names, token material, or unredacted remote broker responses in logs, records, or API responses.
- Run focused tests after each slice, then run the complete Python, web, OpenAPI, Docker, and migration checks before handoff.

## Implementation order

### 1. Retire Railway and fail closed on production configuration

**Files**

- Delete `.github/workflows/provision-customer.yml` and `.github/workflows/update-customer.yml`.
- Modify `app/config.py`, `app/main.py`, `app/provisioning/runs.py`, `app/routers/provisioning.py`, and `app/routers/operator.py`.
- Modify `app/controlplane/rollout_exec.py`, `app/controlplane/pull_reconcile.py`, and `app/controlplane/reconcile_scheduler.py` as required by the preflight and acknowledgement contract.
- Modify `.env.example`, `deploy/broker/broker.env.example`, `deploy/broker/README.md`, `docs/deployment.md`, `docs/hetzner-fleet-architecture.md`, and `.github/workflows/tests.yml`.
- Update provisioning/control-plane regression tests.

**Test first**

- A configuration accepts only `disabled` and `hetzner` provisioner backends; its default is `disabled`, and a customer deployment defaults to `dedicated_server`.
- Production Mission Control startup fails with actionable errors unless it has a Hetzner backend, remote mTLS broker credentials, HTTPS fleet URL, desired-state signing/verification inputs, release verification/promotion/rollback guards, RLS, and automatic reconciliation.
- External provision requests run the same preflight before platform state is created; failed preflight creates no remote-request record.
- No GitHub workflow dispatcher or legacy callback is imported or callable.
- Pull acknowledgement succeeds only for the current attempt, expected release and migration, healthy application result, and expected module versions. Stale or mismatched reports time out as failed rather than succeeding.
- The CI guard fails if either legacy workflow, a Railway backend default, or a user-selectable Railway target is reintroduced.

**Implement**

- Remove GitHub dispatch types and call paths while retaining the per-run Hetzner callback token path.
- Introduce a narrow production-invariant validator in configuration and call it at startup and in provisioning preflight before external mutation.
- Neutralize legacy rollout target naming and reject non-Hetzner targets.
- Make the scheduler an explicit production requirement rather than relying on a manual reconcile endpoint.
- Standardize the broker image setting on `ONEBRAIN_BROKER_IMAGE`.
- Add a non-destructive teardown request/approval/audit protocol only. It must reject live execution until legal-hold and backup evidence plus two distinct approvers are present, and no code path may call `destroy_box`.

**Verification**

- Run `python -m pytest tests/test_provisioning.py tests/test_provisioning_runs.py tests/test_rollout_exec.py tests/test_fleet.py tests/test_fleet_orchestration.py tests/test_hetzner_provisioner.py tests/test_hetzner_remote_broker.py -q`.

### 2. Protect the web boundary, authorization surface, and containers

**Files**

- Modify `onebrain-web/src/app/api/onebrain/[...path]/route.ts` and add a small proxy helper if needed to make streaming and size enforcement testable.
- Modify `onebrain-web/src/lib/login-redirect.ts`, `onebrain-web/src/components/console-shell.tsx`, and any server-side customer route guards that currently rely on broad navigation state.
- Modify `app/config.py`, `app/auth/throttle.py`, `app/deps.py`, and `app/routers/auth.py`.
- Add `migrations/versions/0025_auth_rate_limits.py`, an auth-rate-limit store protocol/implementation, and matching schema validation.
- Modify `Dockerfile`, `Dockerfile.worker`, `onebrain-web/Dockerfile`, and only the application-service sections of rendered Hetzner Compose when required.
- Add a lightweight web test command and focused test files; modify `package.json` and lock files only for the selected test harness.

**Test first**

- `PUT` reaches the API proxy; an oversized declared body receives `413` before an upstream fetch; a chunked body is capped while it streams and is never materialized with `arrayBuffer()`.
- Safe internal paths pass redirect validation. Protocol-relative URLs, backslash paths, cross-origin URLs, script schemes, malformed values, and `/login` return destinations fail closed.
- A customer shell neither renders nor can route to Control/Fleet; operator surfaces remain available only with the server-authorized surface flag.
- Two PostgreSQL-backed API instances see the same account and IP rate-limit counters. The database stores only HMAC/keyed-hash subjects, increments atomically, and bounded expiry cleanup preserves live windows.
- Untrusted forwarded headers cannot choose the rate-limit client key. A configured trusted proxy boundary uses the documented hop/CIDR policy.
- API, worker, and web images run as non-root and start with declared writable state directories.

**Implement**

- Forward all supported verbs, including `PUT`, with Fetch request streaming and a byte-counting transform for bodies without `Content-Length`.
- Normalize redirects against a fixed application origin after rejecting backslashes; require exactly one leading path slash and reject the login route.
- Default `operator_console` to false and make `is_operator_surface` the sole UI/server authorization signal for operator routes.
- Use a PostgreSQL fixed-window store in production and the compatible memory store in local tests. Derive opaque key hashes with a dedicated configured secret; derive client IP only from the peer unless the peer is an explicitly trusted proxy.
- Add non-root users and ownership for only the runtime state/cache locations the images require. Do not add broad read-only filesystem or capability restrictions until their production-like smoke tests pass.

**Verification**

- Run `python -m pytest tests/test_throttle.py tests/test_auth.py tests/test_sessions.py -q` and the web test, lint, typecheck, build, and three Docker builds.

### 3. Add fenced leases and safe recovery to background jobs

**Files**

- Modify `app/jobs/base.py`, `app/jobs/memory.py`, `app/jobs/postgres.py`, `app/jobs/handlers.py`, `app/workers/service.py`, `app/workers/run.py`, `app/config.py`, and `.env.example`.
- Add `migrations/versions/0026_job_leases.py` and update `docs/onebrain-migrations.md`.
- Modify job-producing intake/capture paths only where they need a durable job-ID idempotency key before automatic recovery is enabled.
- Extend `tests/test_jobs.py`, `tests/test_postgres_schema_validation.py`, and add a disposable-PostgreSQL lease-concurrency test.

**Test first**

- Claiming issues a random token and expiry, respects the attempt limit, and atomically reclaims expired running work.
- `renew_lease`, success, failure, and retry updates reject a stale token.
- Lease expiry at the final attempt ends as a sanitized terminal failure.
- A heartbeat keeps an active handler leased; SIGTERM stops new claims while an owned in-flight handler may finalize.
- Replayed or reclaimed handlers use the job ID to avoid duplicate durable intake or capture effects.
- Concurrent PostgreSQL workers cannot own the same active lease.

**Implement**

- Add nullable lease token/expiry fields and indexed atomic claim SQL using the existing `FOR UPDATE SKIP LOCKED` pattern.
- Carry the token through every terminal state transition and use a periodic heartbeat while a worker handler runs.
- Make recovery explicitly at-least-once, with idempotent handler effects and observable lease-expired errors, never an unbounded retry loop.

**Verification**

- Run `python -m pytest tests/test_jobs.py tests/test_postgres_schema_validation.py -q`, then the disposable PostgreSQL concurrency suite.

### 4. Make AI turns recoverable and embedding configuration deterministic

**Files**

- Modify `app/ai_employees/base.py`, `app/ai_employees/memory.py`, `app/ai_employees/postgres.py`, `app/ai_employees/runtime.py`, `app/ai_employees/backends/litellm.py`, and `app/routers/ai_employees.py`.
- Modify `app/embeddings/factory.py`, `app/embeddings/litellm_embedder.py`, `app/deploy/runtime.py`, `app/config.py`, `.env.example`, and embedding deployment documentation.
- Add `migrations/versions/0027_ai_agent_run_leases.py`.
- Modify the browser API/client panel that originates agent-turn idempotency keys.
- Extend AI conversation/scope tests and add focused LiteLLM embedder tests.

**Test first**

- Concurrent requests with one `(tenant, account, space, idempotency key)` atomically create or return one run.
- A disconnect or generator close conditionally marks the owned run cancelled; a provider error marks it failed; neither starts another paid call.
- A reconnect with the same key returns the existing terminal/progress state; an explicit Retry generates a new key.
- A stale owner cannot finalize a reclaimed run. Expired provider-call leases become terminal abandoned or failed records without an automatic model retry.
- The configured embedding dimension reaches LiteLLM unchanged; provider output mismatch, unavailable production provider, and pgvector schema mismatch all fail deterministically.

**Implement**

- Replace lookup-then-insert turn creation with a store-level atomic `begin_or_get` operation and a unique database constraint.
- Add lease token, expiry, and heartbeat fields to AI runs; use `try`/`finally` around stream ownership and token-conditional terminal writes.
- Retain the client idempotency key through reconnects and make retry an explicit UI action.
- Treat embedding dimension as an immutable deployment contract. Validate provider output and production-like LiteLLM/pgvector availability at startup or deployment preflight without silently changing dimensions.

**Verification**

- Run `python -m pytest tests/test_ai_employee_conversations.py tests/test_ai_employee_api_scope.py tests/test_litellm_embedder.py tests/test_pgvector_schema.py tests/test_deploy_runtime.py -q`.

### 5. Lock release inputs, expand CI, and correct operational documentation

**Files**

- Add `requirements.in`, `requirements-dev.in`, and generated exact hash locks; modify `requirements.txt` and add `requirements-dev.txt` as needed.
- Modify `Dockerfile`, `Dockerfile.worker`, `onebrain-web/Dockerfile`, `deploy/broker/docker-compose.yml`, `.github/workflows/tests.yml`, and `.github/workflows/publish-images.yml`.
- Add `.github/dependabot.yml`.
- Modify `scripts/export_openapi.py`, regenerate `onebrain-web/src/lib/openapi.json`, and update web client types if generated API shapes change.
- Modify `tests/test_box_update_sh.py`, deployment/migration/operator docs, and any workflow tests that assert legacy behavior.

**Test first**

- A fresh hash-locked install succeeds only from the generated inputs; the resolved environment passes `pip-audit`.
- Docker base images and GitHub Actions are immutable references; Dependabot covers Python, npm, Docker, and Actions inputs.
- OpenAPI export in check mode exits nonzero when the checked-in schema is stale, including `must_change_password`.
- Shell tests skip only when `bash --version` proves unavailable and run on the Linux CI lane.
- A PostgreSQL/pgvector CI service validates migrations, RLS, shared throttle, lease fencing, and concurrent idempotency paths.
- Docker CI builds all three images and performs a minimal non-root start smoke.

**Implement**

- Resolve and audit patched dependency versions with a reproducible compiler; use `--require-hashes` in CI/container builds where compatible.
- Pin image digests and action SHAs from their authoritative upstreams, then configure automated update proposals.
- Add an OpenAPI `--check` mode and make freshness a required CI step.
- Use a Linux PostgreSQL/pgvector integration job and retain fast local unit tests. Keep test databases explicitly disposable and never inherit a production DSN.
- Document the Hetzner-only control plane, broker custody and mTLS setup, production invariant configuration, lease semantics, embedding migration contract, disabled teardown protocol, and the external canary/rollback/backup-restore checklist.

**Verification**

- Run `python -m pip check`, `python -m pytest -q -p no:cacheprovider`, the OpenAPI check, `npm ci`, web lint/typecheck/build, Docker builds, and `git diff --check`.

The CI-only Linux, pgvector, and browser/container smoke lanes will be checked for valid configuration locally and then observed in GitHub Actions after a safe push. They are not treated as passing merely because this Windows host cannot run every service identically.

## Final release and activation sequence

1. Re-run the complete verification gate and inspect every task-related diff.
2. Stage only task files, preserving unrelated local changes.
3. Commit the verified hardening work on `codex/hetzner-production-hardening`.
4. Push and merge only if the worktree is free of unrelated changes and all checks pass; otherwise report the exact shipping blocker without modifying the unrelated work.
5. Before enabling production behavior, an operator must revoke Railway credentials/workflow permissions, deploy the mTLS broker and firewall, configure invariant settings, and pass canary provision/update/rollback, tenant-isolation, and backup/restore drills.

## Done criteria

- No executable Railway production path remains.
- Multi-replica API instances share secure authentication limits and preserve tenant/operator boundaries.
- Jobs and AI turns recover safely from crashes without stale-owner writes or automatic duplicate paid calls.
- Embedding dimension/provider errors fail before unsafe runtime operation.
- Dependencies, actions, images, API contract, Linux scripts, database migrations, and containers are independently verified by reproducible gates.
- Remaining live-environment steps are documented as explicit operator actions, not silently assumed complete.
