# Production Activation and Recovery Runbook

This is the active operator checklist for enabling a multi-replica production
deployment. It covers operator-owned infrastructure and credential actions that
cannot be performed safely by application code. Do not treat a passing local or
CI test run as permission to skip any checkpoint below.

The active production path is Mission Control (MC) -> private Hetzner broker ->
isolated development gate and customer deployments. Railway is not a supported
control-plane, provisioning, or rollout path.

## 1. Retire the former Railway access path

Complete these actions in the Railway and GitHub organizations, record the
ticket/change reference, and have a second operator review the result:

1. Revoke Railway API tokens, service-account credentials, deploy keys, build
   hooks, integrations, and environment variables that could deploy or read a
   OneBrain environment.
2. Delete Railway-related GitHub Actions secrets, variables, environment
   protections, and deployment permissions. The repository no longer contains
   customer-provisioning or customer-update Railway workflows; remove any
   copied, manually created, or organization-level equivalent as well.
3. Remove GitHub App, webhook, and personal-access-token grants that gave a
   Railway integration access to this repository or its packages.
4. Verify the audit logs show no remaining active Railway principal capable of
   changing a OneBrain deployment.

Do not use this retirement task to delete a customer environment. Customer
teardown remains deliberately disabled; see [Teardown review is not
execution](#6-teardown-review-is-not-execution).

## 2. Establish the private Hetzner broker boundary

Before enabling MC-managed provisioning, complete the dedicated broker host
runbook in [the broker bundle](../deploy/broker/README.md) and verify all of
these facts:

- The Hetzner Cloud token exists only in the root-owned broker environment. MC,
  CI, the development gate, and customer hosts do not receive it.
- The broker process is loopback-only. Its TLS front end requires an MC client
  certificate signed by the installed client CA, verifies the host name, and
  separately checks the broker credential.
- The Hetzner firewall is default-deny and permits TCP 443 only from the
  documented MC egress address or addresses. SSH is a separate reviewed
  break-glass path, not a general ingress rule.
- A negative test from an untrusted address and a test without an MC client
  certificate both fail. A valid MC certificate with an invalid credential also
  fails.
- MC is configured with the HTTPS broker URL, the broker credential, and
  readable mTLS client certificate/key (and CA when required). It has no
  `ONEBRAIN_HETZNER_API_TOKEN` value.

The production MC preflight must pass before the first provision request. A
missing broker, failed mTLS verification, invalid desired-state signing setup,
or missing release/RLS prerequisite blocks provisioning rather than using a
fallback cloud credential.

### Bootstrap the first Mission Control box

`scripts/bootstrap_mc.py` is the only exception to the normal token boundary:
run it from a controlled, short-lived operator workstation with the initial
Hetzner token available only for that create request. The rendered MC must not
retain that token or a DNS token derived from it; once it has booted, every
subsequent provisioning request goes through the remote broker.

Before a production bootstrap, prepare all of the following and keep evidence
with the change record:

1. A public, valid FQDN whose HTTPS public URL has the same host, a Hetzner DNS
   zone/base domain, and a DNS record path that the bootstrap can manage. Do
   not accept a raw-IP or HTTP-only MC endpoint: secure browser cookies and
   fleet callbacks require TLS.
2. A pre-created, reviewed MC firewall. Its allowlist must cover only the
   required customer-box/broker/operator paths (or a separately reviewed
   front-door/VPN), plus the certificate-validation path where applicable. The
   bootstrap must not create the generic public customer firewall for MC.
3. A unique broker credential and a unique client certificate/key, plus the
   broker CA when private trust is used. Use compact, current ECDSA material
   where possible: cloud-init has a hard user-data size limit. The key is
   copied root-only and mounted read-only only into the MC API container.
4. An escrowed, valid `ONEBRAIN_SECRET_ENCRYPTION_KEY` (Fernet key or 32-byte
   hex key), desired-state private key, offline release-signing custody record,
   release verification key/allowlist, and a positive reconciliation interval.
   Losing the Fernet or desired-state private key can make existing customer
   bundles unrecoverable.

Run the script's dry-run first and confirm its output redacts every secret and
the mTLS archive. On the live create path, save the one-time MC admin credential
only in the approved secret store, then remove the initial Hetzner token from
the bootstrap workstation.

## 3. Scale API and worker replicas without weakening isolation or authentication

Run `alembic upgrade head` with the migration-owner role before adding new API
or worker replicas. Production API replicas use the same PostgreSQL
`auth_rate_limits` table for failed-login counters; there is no per-process
production limiter.

For the durable-job role split, complete this sequence before any replica
receives production traffic:

1. On a fresh box, set `POSTGRES_APP_ROLE`, `POSTGRES_APP_PASSWORD`,
   `POSTGRES_WORKER_ROLE`, `POSTGRES_WORKER_PASSWORD`,
   `POSTGRES_ASSISTANT_ROLE`, `POSTGRES_ASSISTANT_PASSWORD`,
   `POSTGRES_COMMUNICATION_ROLE`, and `POSTGRES_COMMUNICATION_PASSWORD` before
   Postgres first initializes. The bundled `postgres-init.sh` creates distinct
   `NOSUPERUSER NOBYPASSRLS NOINHERIT` logins. The `postgres-roles` one-shot
   service repeats the idempotent ACL normalization on existing volumes before
   migrations, so all four runtime identities are limited to their intended
   product database.
   Before updating an existing box that has an older sealed bundle, call
   `POST /api/fleet/deployments/{id}/backfill-runtime-db-credentials` as an
   operator admin. It adds only missing restricted runtime passwords, re-seals the
   bundle on MC, and bumps its epoch; wait for the box to report that epoch
   before applying the Compose/release update. It returns no secret values and
   is safe to retry.
2. Run the owner-role migration with `ONEBRAIN_POSTGRES_APP_ROLE` and
   `ONEBRAIN_POSTGRES_WORKER_ROLE` set, through
   `0029_job_queue_rls_roles` or later.
3. Give API replicas the app-role `ONEBRAIN_DATABASE_URL` and the two role
   names, but not `ONEBRAIN_WORKER_DATABASE_URL` or its password. Give workers
   the same app DSN/names plus their distinct worker-role
   `ONEBRAIN_WORKER_DATABASE_URL`.
   Assistant and Communication services receive only their own product-local
   DSN/password; none of the long-running services receives the migration-owner
   password.
4. Before scale-out, verify from the app login that a same-scope job can be
   created/read while `jobs.payload` and `job_files` reads are denied and a
   different tenant's job is invisible. Verify from the worker login that a
   claim/file read succeeds but a normal application-table read and a payload
   update are denied. A role validation failure blocks startup; do not bypass
   it with an owner or operator DSN.

- Set a unique `ONEBRAIN_LOGIN_RATE_LIMIT_SECRET` of at least 32 characters.
  The table stores HMAC hashes of normalized account identifiers and client
  addresses, not the raw values.
- Set `ONEBRAIN_TRUSTED_PROXY_CIDRS` and
  `ONEBRAIN_TRUSTED_PROXY_HOPS` only when the direct peer is a controlled proxy
  in those CIDRs. Otherwise leave the hop count at zero: arbitrary
  `X-Forwarded-For` headers must not select a rate-limit bucket.
- Bring up at least two API replicas and confirm failed login attempts against
  the same account and the same client address are limited across both
  replicas. Confirm a forged forwarding header cannot evade that limit.
- Alert on a sustained increase in login lockouts, but do not log passwords,
  raw account addresses, or raw client addresses as the rate-limit subject.

Production customer provisioning requires a Hetzner-managed DNS zone, valid
base domain, and resulting FQDN. A configuration that would serve a customer
on a raw IP / HTTP-only Caddy endpoint is rejected before server creation.

## 4. Validate durable work and the embedding contract

Background jobs and direct AI Employee turns are at-least-once operations with
fenced, expiring leases. Do not manually mark a running row successful or
failed unless an incident procedure establishes that the current lease owner
has stopped. A stale worker or stream must not overwrite a reclaimed owner.

- Before allowing a handler to be reclaimed, make the handler's external effect
  idempotent by its job ID. Monitor lease-loss events, jobs at their retry
  limit, and AI turns that expire or lose a lease.
- Preserve an AI turn's idempotency key when a browser reconnects. A new paid
  model call requires an intentional new key. Keep the configured provider
  timeout shorter than the AI run lease so heartbeats have time to run.
- For a production LiteLLM + pgvector deployment, choose
  `ONEBRAIN_EMBEDDING_DIM` deliberately and keep it immutable for that vector
  set. Startup preflight verifies both provider availability/output dimension
  and the migrated pgvector column dimension before serving traffic.
- Change an embedding model or dimension only through a versioned re-embedding
  migration: retain the old vectors until the new set and its indexes are
  verified, then switch retrieval deliberately. Never repair a mismatch by
  dropping `chunks` or by accepting a provider-selected dimension.

## 5. Canary, update, rollback, restore, and isolation proof

Run this sequence with dummy data before enabling real customer creation and
again after material broker, release, database, or network-policy changes:

1. Provision an isolated dummy-data development-gate/customer-shaped canary
   through the broker. Verify the broker audit trail and that MC received only
   sanitized deployment metadata.
2. Promote a signed, digest-pinned release through the development gate. Check
   the reported rollout attempt, exact release, migration, enabled-module
   versions, and application health; a stale or mismatched report must remain
   pending and eventually fail rather than complete the rollout.
3. Perform one explicit customer-shaped update canary, then exercise the
   recorded rollback path. For a `restore_required` release, restore the
   tested recovery point instead of assuming that a code rollback reverses a
   schema change.
4. Rehearse backup restoration into a separate, access-restricted target.
   Record recovery-point and recovery-time results, verify application health,
   and remove the rehearsal target according to the approved data-retention
   process.
5. Prove tenant isolation with negative tests: an account cannot query another
   account/workspace through the application or database role; customer ingress
   has no operator, fleet, provisioning, or rollout route; and no customer
   host receives another customer's database credentials, service key, queue,
   or MC/broker credential. Include the queue-role proof: an API login cannot
   read queued payloads or files, and a worker login cannot use its
   cross-tenant claim capability to read a general application table.

Do not broaden rollout scope until each result is recorded and reviewed. A
failed canary pauses the release and preserves the last known-good deployment;
it never triggers a fleet-wide automatic rollout or automatic destructive
action.

## 6. Teardown review is not execution

The operator teardown endpoints create a non-destructive review record only.
They do not call the broker, Hetzner, a cloud API, or a customer host to delete
anything.

To create a review record, an authorized operator supplies the deployment and
account binding plus references to legal-hold and backup/retention evidence. An
active platform legal hold blocks both request and approval. The API returns a
short-lived approval nonce once; only its hash is stored.

Two distinct authenticated approvers, neither of whom is the requester, must
approve with the nonce before the record reaches its terminal state. That state
is explicitly `execution_disabled` and records that no customer resources were
deleted. An expired nonce also ends in a non-execution result. Each request and
denial/approval is appended to the platform audit trail.

Live teardown requires a separately reviewed design, a dedicated executor,
external legal/retention authority, and a new activation decision. Do not infer
permission for deletion from a completed review record.

## 7. Ongoing operational signals

Monitor and alert on:

- broker health, mTLS/authentication failures, and firewall-policy changes;
- reconciliation deadline failures, stale/mismatched customer rollout reports,
  and development-gate health;
- backup freshness, failed backup jobs, and restore-rehearsal age;
- exhausted job attempts, expired/lost job leases, and expired/lost AI-run
  leases;
- sustained shared-login-limit lockouts; and
- database/RLS errors, pgvector dimension preflight failures, and attempted
  customer access to control-plane routes.

Review this runbook after every recovery rehearsal and whenever the external
broker, firewall, proxy, identity provider, backup platform, or release trust
chain changes.
