# Hetzner Production Hardening Design

**Date:** 2026-07-17
**Status:** Approved for written-spec review
**Scope:** Production hardening after the repository audit, with support for multiple API replicas.

## Purpose

Make OneBrain safe and reproducible to operate as a multi-tenant, multi-replica production service on Hetzner. The work removes the deprecated Railway deployment path, closes confirmed web and authorization defects, makes background and AI work recoverable, and turns the current successful local checks into meaningful production gates.

This is a coordinated hardening program, not a single risky cutover. It will be delivered as independently testable workstreams under one compatibility contract.

## Goals and boundaries

The completed system must meet these properties:

- Production provisioning and rollout use the Hetzner pull model only; no GitHub-dispatched Railway workflow remains reachable.
- A request may reach any API replica without weakening authentication throttling, tenant isolation, or idempotency.
- A process crash, deploy interruption, or client disconnect cannot leave a job or AI turn permanently running.
- Customer-facing surfaces cannot expose operator navigation or accept an external login return URL.
- Builds use reproducible, audited dependencies and immutable CI/container inputs.
- The repository proves these properties through automated Linux, Docker, PostgreSQL, API-contract, and focused browser-boundary tests.

The change deliberately does **not** delete live infrastructure, rotate real secrets, or rename historical `railway_*` database columns. Live teardown stays disabled until the organization configures legal-hold, backup-retention, and two-person approval controls outside the application. The retained column names are compatibility storage for existing Hetzner coordinates and will be renamed only in a separately inventoried migration.

## Selected architecture

### 1. Hetzner-only control plane

Railway is fully retired from executable deployment behavior:

- Delete the `provision-customer` and `update-customer` GitHub Actions workflows and remove GitHub workflow dispatchers, callbacks, settings, routing call sites, and customer-facing target choices.
- Restrict `provisioner_backend` to `disabled` or `hetzner`, defaulting to `disabled`. Validate all Hetzner prerequisites before creating external platform state, so a misconfigured control plane fails before a partial provision.
- Preserve the existing Hetzner pull broker. Production validation requires a Hetzner backend, remote broker URL and credentials, mTLS client certificate and key, an HTTPS fleet URL, desired-state signing/verification configuration, registry and promotion settings, signed release and rollback metadata, RLS, and an enabled reconciliation scheduler.
- Treat an update as complete only when the broker report names the current attempt, exact release and migration versions, expected enabled-module versions, and a healthy application result. A stale, malformed, or mismatched report remains pending until its deadline, then fails explicitly.
- Align the broker Compose variable and its example environment file on `ONEBRAIN_BROKER_IMAGE`.

The chosen approach is full retirement rather than leaving an inactive compatibility switch. It removes a second, untested production path while preserving persisted identifiers until a safe data migration can be planned.

### 2. Multi-replica web and tenant security

The API remains the enforcement point; the Next.js proxy only transports requests safely.

- Proxy all supported verbs, including `PUT`, and stream request bodies to FastAPI instead of buffering them in memory. Enforce declared size before forwarding and enforce a byte-counting limit for chunked requests with no `Content-Length`.
- Replace the in-memory login limiter in production with a PostgreSQL-backed, expiry-window counter. Store only keyed hashes of normalized account identifiers and client addresses. Increment account and IP keys atomically so every API replica sees the same limit, and clean expired rows in bounded batches.
- Obtain the client address only from the direct peer unless an explicitly configured, trusted proxy-hop boundary is in use. Never trust arbitrary forwarded headers.
- Reject login return values containing backslashes, protocol-relative paths, foreign origins, or the login route itself. The client may navigate only to a normalized same-origin application path.
- Default `operator_console` to false. Render customer navigation from `is_operator_surface`, not a broad non-operator fallback, and protect customer routes server-side as well as in the shell.
- Run API, worker, and web images as non-root users. Keep mounts writable only where required and prove the resulting containers start and write their declared state paths.

PostgreSQL is selected for rate limiting because it is already the required production shared store. It avoids a new Redis dependency while giving all replicas one transactional source of truth.

### 3. Recoverable jobs and AI turns

Durable work uses lease ownership and idempotent effects rather than assuming a process survives.

- Add a random `lease_token` and `lease_expires_at` to background jobs. Claiming atomically acquires queued, retryable, or expired work subject to the attempt limit. The worker heartbeats the lease while a handler runs, stops claiming on termination, and conditionally completes or fails only when it still owns the token.
- Make each handler's external effects idempotent by the job ID before allowing lease recovery. A reclaimed job can safely run at least once; a stale worker can never overwrite the current owner's terminal state.
- Create or return an AI agent run atomically for `(tenant, account, space, idempotency key)`, with the same lease/fencing discipline. `try`/`finally` terminalization handles cancellation and generator shutdown. A disconnect cancels the stream without silently starting another paid model call.
- Keep the browser idempotency key across reconnects until the run reaches a terminal state; an intentional retry generates a new key.
- Make embedding dimensions configuration-driven and immutable at runtime. The LiteLLM/pgvector production preflight verifies provider availability and schema dimension before accepting traffic; it fails closed instead of probing and silently mutating dimensions.

This is an at-least-once model. It does not claim exactly-once execution; idempotency and fencing make retries safe and observable.

### 4. Reproducible supply chain and delivery gates

Production inputs become explicit and checked continuously:

- Replace lower-bound-only Python requirements with compiled, hash-locked runtime and development lock files sourced from concise input files. Update vulnerable resolved packages and run `pip-audit` against the locked environment.
- Pin Docker base images and GitHub Actions to immutable digests/commit SHAs. Add Dependabot coverage for the locked ecosystems and pinned actions.
- Regenerate the checked-in web OpenAPI document and fail CI when the generated output differs from the committed contract.
- Run Python tests on Linux, including a real PostgreSQL/pgvector integration lane, shell tests only when a functioning Bash exists, Docker image build/start smoke checks, and the existing web lint/typecheck/build checks. Add focused automated coverage for proxy streaming, redirect validation, customer navigation, rate limiting, leases, stale owners, retry limits, and AI reconnect behavior.
- Correct deployment, broker, migration, and operator documentation to describe the Hetzner-only architecture, actual PostgreSQL control-plane state, limits of the local test environment, and the external production checklist.

### 5. Safe operational lifecycle

The application gains a documented teardown protocol but no live deletion capability is enabled by default. A destructive teardown request must remain rejected unless an external policy layer supplies a legal-hold decision, verified backup/retention record, and two independently authenticated approvals. The initial implementation supplies the state model, validation boundary, audit trail, and tests; an operator must complete the external prerequisites before live deletion is considered.

The deployment runbook will also require operators to:

1. Remove or revoke the existing Railway GitHub secrets and legacy workflow permissions outside the repository.
2. Deploy and firewall the Hetzner broker with mTLS credentials, a scoped token, and restricted management access.
3. Configure all multi-replica production settings and confirm the control-plane preflight passes.
4. Perform a canary provision, update, rollback, backup/restore rehearsal, and tenant-isolation verification before broad rollout.
5. Set monitoring for reconciliation deadline failures, exhausted job/AI leases, authentication-rate-limit spikes, broker health, and backup freshness.

## Data and compatibility changes

New migrations are additive and forward-compatible:

- A job-lease migration adds lease token and expiry data plus indexed claiming support.
- An AI-run lease migration adds ownership, expiry, heartbeat, and terminal-result fields required for idempotent reconnects.
- A rate-limit migration adds expiry-window counters with an indexed hashed subject, scope, and window identity.
- A teardown-protocol migration adds a non-destructive `customer_teardown_requests` record with its requested target, legal-hold evidence reference, backup/retention evidence reference, two approver identities, decision timestamps, and immutable audit events. It contains no live-delete executor.

Existing data remains readable during rollout. The application must tolerate null lease data for records created before migration and only require leases for newly claimed work. Database migrations run before pods are scaled to the new workers. No migration drops or renames a production column in this program.

## Delivery order

1. Retire Railway behavior and add Hetzner configuration/preflight guards.
2. Close HTTP proxy, redirect, navigation, operator-default, and container-user boundaries.
3. Add shared authentication limiting and its PostgreSQL migration.
4. Add job leases/idempotent effects, then AI run leases/reconnect behavior and strict embedding validation.
5. Lock and audit dependencies; pin CI/container inputs; add contract, Linux, Docker, and PostgreSQL gates.
6. Update runbooks and execute the external canary/rollback/restore checklist.

Each workstream must be independently reviewable and green before the next is promoted. The implementation plan will name exact file changes, migrations, rollback conditions, and verification commands for each step.

## Acceptance criteria

- Repository search finds no executable Railway workflow, dispatcher, backend selection, default, or user-facing deployment choice.
- A production configuration without every Hetzner and multi-replica prerequisite fails both startup validation and the provisioning preflight with actionable errors, before external mutation.
- Two API replicas enforce the same account and IP login limits through PostgreSQL, while untrusted forwarding headers cannot select another client's identity.
- Oversized or chunked proxy bodies never require full in-memory buffering; valid `PUT` requests reach the API.
- Customer users cannot see or reach operator surfaces, and every tested redirect resolves to a same-origin non-login route.
- A killed worker or interrupted AI stream recovers safely after lease expiry; stale owners cannot overwrite a reclaimed result; retry keys do not duplicate paid work.
- LiteLLM/pgvector dimension/provider mismatches fail preflight deterministically.
- Fresh CI installs from locked files, passes audit, detects stale OpenAPI, and exercises Linux Bash, Docker, and PostgreSQL paths.
- Documentation identifies the remaining external operator actions explicitly and contains no contradictory Railway production instructions.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Schema migration or version skew during a rolling deploy | Use additive migrations, nullable compatibility reads, migrate before scaling workers, and retain a tested rollback procedure. |
| Lease expiry duplicates an external effect | Require job-ID idempotency before reclaiming work and fence all terminal writes with the lease token. |
| Rate-limit table becomes a hot path | Use narrow indexed rows, atomic upserts, fixed windows, bounded expiry cleanup, and rate-limit only authentication endpoints. |
| Full Railway removal leaves undiscovered operator automation | Search repository and organization configuration, document external revocation, and canary Hetzner provisioning before cutover. |
| Non-root containers expose mount permission problems | Use explicit writable directories and container smoke tests before enabling any stricter read-only filesystem policy. |

## Decisions already made

- Production is designed for more than one API replica.
- Railway is removed rather than kept as disabled compatibility code.
- PostgreSQL is the shared limiter store; no Redis service is introduced for this purpose.
- Live infrastructure deletion remains disabled pending independently configured governance controls.
- Historical `railway_*` column names remain unchanged in this hardening program.
- There are no unresolved product decisions in this design; credentials and live-environment actions remain operator-owned execution steps.
