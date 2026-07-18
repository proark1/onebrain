# Mission Control user management design

## Status

Approved in conversation on 2026-07-18.

## Goal

Give Mission Control (MC) administrators a secure, deployment-scoped way to
manage human accounts on customer servers. An operator can inspect a server's
user directory, create an account, disable or re-enable login, safely delete an
account, or issue a one-time password. A user who receives a one-time password
must replace it before using the product.

The design extends the existing `must_change_password` and session-revocation
behavior. It does not create a second authentication system or give MC direct
database access.

## Scope

- Add an operator-only **Users** destination to the MC console.
- Select exactly one registered customer deployment before reading or changing
  users.
- List active and disabled users and optionally show anonymized deleted users.
- Create users with a server-generated one-time password.
- Reset an existing user's password to a server-generated one-time password.
- Disable and re-enable accounts.
- Safely delete accounts by revoking access and anonymizing identity while
  preserving company content and audit continuity.
- Carry operations over a deployment-pinned, outbound pull channel.
- Show remote progress and typed failures in MC.
- Audit every management operation on MC and on the customer deployment.

## Non-goals

- Email or SMS password recovery.
- Letting an operator choose or transmit a password.
- Bulk user operations or cross-deployment actions.
- Editing an existing user's email, name, role, or location in this version.
- Reassigning workspace ownership from MC. Ownership blockers identify what
  must be reassigned in the customer administration surface before deletion.
- Erasing company records, authored content, or audit history. Personal-data
  erasure remains a separate Privacy workflow.
- Exposing a customer database, Docker socket, management API, or MC credential
  to the public network or customer-facing containers.

## Chosen architecture

MC uses an outbound, pull-based management-job channel. MC never connects to a
customer database or calls a public administration endpoint. A root-owned host
agent on each compatible deployment pulls a narrowly typed command, verifies
it, and passes structured JSON on standard input to a user-management CLI inside
the OneBrain API container. The CLI is the only new customer-side execution
surface.

```text
MC operator UI
  -> operator-only MC API
  -> encrypted, deployment-scoped job

Customer host agent (outbound HTTPS, deployment fleet credential)
  -> pulls one signed command for its own deployment
  -> verifies contract, signature, deployment, expiry, and action allowlist
  -> sends JSON over stdin to the internal OneBrain user-management CLI
  -> posts an end-to-end encrypted result to MC

MC API
  -> records the encrypted result
  -> shows directory data for a short cache period
  -> consumes a password result exactly once
```

This preserves the existing customer boundary: no fleet or operator route is
mounted in the customer application and the public edge continues to reject
control-plane paths.

## Trust and authorization

### MC operator authorization

Directory reads require an authenticated MC human with the `admin` role and
operator mode enabled. Every mutation also requires a recent MC session. A
session older than 15 minutes receives `recent_authentication_required`; the
operator signs in again and deliberately retries the action.

Every API resolves the deployment through the control-plane registry. Request
payloads cannot override the deployment, tenant, or fleet credential scope.

### Deployment authentication

The host agent authenticates with its existing deployment-pinned fleet key. A
key can pull and submit only jobs whose `deployment_id` equals the deployment
bound to that key. The new endpoints do not make one fleet key valid for another
deployment.

### Command authenticity

MC signs a canonical, domain-separated `user-management-command.v1` envelope
with the existing desired-state signing trust. Reusing this trust does not add a
stronger privilege: that signer can already select and deploy customer code.
Domain separation prevents a command from being interpreted as desired state,
or vice versa.

The agent verifies all of the following before execution:

- contract is exactly `user-management-command.v1`;
- signature is valid under a currently trusted MC desired-state public key;
- command deployment matches the local deployment;
- action is one of the six allowed operations;
- command has not expired;
- command ID and idempotency key are well formed;
- payload has exactly the fields permitted for that action.

Unknown fields, free-form executable content, shell fragments, and unrecognized
actions fail closed. User-controlled values travel in JSON on standard input,
never in shell arguments.

## Command and result contracts

The allowed actions are:

| Action | Purpose | Mutation |
| --- | --- | --- |
| `directory.snapshot` | Return users plus assignable roles and locations | No |
| `user.create` | Create an active user with a one-time password | Yes |
| `user.password.reset` | Replace a password and force rotation | Yes |
| `user.disable` | Disable login and revoke sessions | Yes |
| `user.enable` | Re-enable a disabled user | Yes |
| `user.delete` | Revoke access and anonymize a disabled user | Yes |

Commands contain an opaque target user ID after directory discovery. Email is
accepted only by `user.create`; later actions do not identify a user by email.
Tenant is never a command field. The customer service pins new users to the
deployment's configured human tenant.

Mutation commands expire 15 minutes after creation. Directory refresh commands
expire after 5 minutes. An expired command is never revived. Retrying in MC
creates a new command with a new ID and an explicit audit event.

Jobs progress through `queued`, `leased`, `completed`, `failed`, or `expired`.
A lease has a short deadline so an agent crash permits another pull. The job's
idempotency key and the customer receipt ledger make repeat delivery safe.

Customer errors use a fixed code allowlist, including:

- `duplicate_email`;
- `invalid_role`;
- `invalid_location`;
- `user_not_found`;
- `invalid_state_transition`;
- `last_active_admin`;
- `ownership_reassignment_required`;
- `command_expired`;
- `command_replayed`;
- `capability_unavailable`;
- `internal_failure`.

Raw exception text never crosses the fleet boundary or enters MC logs.

## Encryption and one-time password custody

MC seals each command payload at rest with the existing MC one-time-secret
cipher. Only minimal routing metadata—job ID, deployment ID, action, state, and
timestamps—remains queryable. MC decrypts the payload only while serving it to
the authenticated deployment over TLS.

For every command, MC creates an ephemeral X25519 result key pair. The public
key is included in the signed command. The private key is sealed at rest with
the MC one-time-secret cipher. The customer derives an AES-256-GCM result key
with X25519 and HKDF-SHA256, then encrypts the result using the command contract,
job ID, deployment ID, and action as authenticated associated data. The
customer's ephemeral public key, nonce, and ciphertext are safe to retry and
store; plaintext directory data and passwords are not.

Create and reset passwords are generated on the customer server with a
cryptographically secure generator and satisfy the current password policy.
Only the password hash is written to `users`. The plaintext exists briefly in
the customer process that encrypts the result and in the MC process that
performs the one-time reveal.

A password result expires 10 minutes after MC receives it. Revealing it uses a
row lock: MC decrypts successfully, clears the encrypted result and sealed
private key, marks the result consumed, commits, and only then returns the
plaintext to the one requesting browser. Concurrent or later reveal requests
receive `secret_already_consumed`. Refreshing the page cannot reveal it again.
Dismissal clears the browser's in-memory value.

Directory snapshots remain encrypted and readable to authorized MC operators
for 15 minutes, then are purged. They are never added to fleet heartbeats.

## Crash safety and idempotency

The customer database records a `user_management_receipt` keyed by command ID.
For a mutation, the user change, session revocations, local audit event, and
encrypted result receipt commit in one database transaction. A replay returns
the stored encrypted result without executing the mutation again. This closes
the crash window between changing a password and returning its one-time value.

The host agent also writes encrypted results to a root-owned retry outbox using
an atomic temporary-file rename. Directory permissions are `0700` and files are
`0600`. The outbox contains only end-to-end ciphertext. The agent retries the
same result until MC acknowledges it, then removes the file. Customer receipts
and stale outbox entries are purged after 24 hours; after MC consumes an
ephemeral private key, retained ciphertext cannot recover a password.

## Customer account rules

### Directory

A directory snapshot returns only fields needed by this feature:

- opaque user ID;
- display name and normalized email;
- role and location;
- `active`, `disabled`, or `deleted` status;
- `must_change_password`;
- created timestamp;
- whether deletion is currently blocked and the blocker category.

It also returns server-authoritative assignable roles and allowed locations. MC
does not maintain its own role list. Service and public roles are not assignable
to human users.

### Create

Create accepts display name, email, role, and location. The customer server
normalizes and validates the email, validates role/location compatibility,
creates an active user in the local tenant, sets `must_change_password=true`,
and returns a generated one-time password. Duplicate normalized emails fail
without changing any data.

### Password reset

Reset is allowed for an active or disabled non-deleted user. It replaces the
hash, sets `must_change_password=true`, and revokes all active sessions. Reset
does not implicitly enable a disabled account. When an active user signs in
with the one-time password, the existing principal gate permits only session
inspection, password change, and logout until rotation succeeds. Successful
self-service rotation clears the flag and revokes the one-time-password
session, as it does today.

### Disable and enable

Disable changes an active user to `disabled` and revokes all sessions in the
same transaction. It is rejected if the target is the last active administrator
on the deployment. Repeated disable requests return the original successful
receipt for the same command but a new command against an already disabled user
returns `invalid_state_transition`.

Enable changes only `disabled` to `active`; deleted users cannot be restored.
It does not change the password or `must_change_password` flag.

### Safe deletion

Delete is accepted only for a disabled user. It is rejected when the user owns
an account, personal/family space, or another ownership-bearing resource. The
failure returns encrypted blocker types and safe resource identifiers so the
operator can direct a customer administrator to the correct reassignment
surface. It never returns private content.

After blockers are removed, deletion atomically:

- revokes any remaining sessions;
- changes status to `deleted`;
- replaces email with a unique non-routable value based on the opaque user ID;
- replaces display name with `Deleted user`;
- replaces the password hash with a random unrecoverable value;
- clears `must_change_password`;
- changes role to the non-privileged public fallback and clears location;
- preserves the opaque user ID, tenant association, timestamps, authored
  company records, and append-only audit references.

Deleted accounts are hidden by default in MC and can be included for audit
inspection. Their original identity cannot be recovered or re-enabled. The old
email can be used to create a new account because the deleted row no longer
holds it.

The last-active-admin check and ownership check run under the same transactional
locking boundary as the status change, preventing concurrent requests from
removing every administrator or racing a newly created ownership reference.

## Mission Control experience

The MC-only navigation gains **Users**. The page begins with a deployment
selector populated from the existing fleet registry. No user request is sent
until one deployment is selected.

The directory table shows name, email, role, location, status, password-change
requirement, and creation time. A manual refresh creates a directory snapshot
job. The page shows queued, running, completed, expired, offline, upgrade
required, and policy-rejected states honestly.

Available actions depend on current state:

- **Create user** asks for name, email, server-provided role, and compatible
  location.
- **Reset password** requires confirmation and explains that all sessions will
  end immediately.
- **Disable** and **Re-enable** state their login effect before confirmation.
- **Delete** is shown only for disabled users, requires typing the user's email,
  and explains anonymization and ownership blockers.

Create and reset completion opens a one-time reveal panel with **Copy** and
**I have saved it**. The backend secret is already consumed when displayed; the
button clears the browser copy. Passwords are not put in URLs, browser storage,
analytics, error reports, React persistence, or logs.

Only one mutation for a target user may be pending at a time. Controls remain
disabled until the job completes, fails, or expires. Switching deployments
clears directory and secret state from the page.

## Persistence

MC adds a durable `fleet_user_management_jobs` table with:

- job ID, deployment ID, action, status, and idempotency key;
- requesting MC user ID and lifecycle timestamps;
- lease owner/deadline and bounded attempt count;
- sealed request payload;
- sealed MC ephemeral private key;
- result sender public key, nonce, and ciphertext;
- fixed error code;
- result expiry and consumed timestamp.

The table contains no plaintext user directory, email, name, password, password
hash, or free-form customer error. PostgreSQL indexes support deployment/status
leasing, operator polling, and expiry cleanup. The memory implementation mirrors
the contract for local tests.

Customer PostgreSQL adds `user_management_receipts` with command ID, action,
encrypted result fields, and execution/expiry timestamps. It contains no
plaintext password or directory. The memory implementation provides equivalent
locking and replay behavior.

## Auditing and privacy

MC and customer audit events include:

- actor ID;
- deployment ID where applicable;
- command ID;
- action;
- opaque target user ID;
- outcome/error code;
- timestamp.

Audit events never include a password, hash, email, display name, encrypted
payload, encryption key, or raw exception. Directory reads are audited as well
as mutations. MC application logs use the same opaque identifiers.

## Capability and rollout

Compatible customer releases report `user_management_v1` as a boolean
capability in metadata-only fleet health. They do not report user identities or
commands in heartbeats. MC keeps the Users controls read-only and displays
**Upgrade required** until the latest fresh heartbeat advertises the capability.

Rollout order is:

1. ship the customer database migration, transaction service/CLI, result
   encryption, host agent poller, and cleanup jobs;
2. verify public-route isolation and capability reporting on the development
   gate;
3. ship the MC job migration, store, agent endpoints, operator endpoints, and
   cleanup;
4. enable the MC Users interface;
5. canary directory, create, reset, disable/enable, and safe deletion on a
   synthetic development user before customer use.

Older deployments continue normal heartbeats and product use. They cannot
receive a user command and MC does not silently fall back to SSH, direct SQL, or
a public API.

## Failure behavior

- Offline deployment: the job stays queued only until its explicit expiry; MC
  offers a deliberate retry after fleet health returns.
- Invalid signature, scope, contract, expiry, or action: the agent refuses to
  invoke the CLI and reports an allowlisted rejection.
- Lost pull response: the lease expires and the same command can be delivered
  again.
- Crash during mutation: the database transaction commits both the change and
  encrypted receipt or neither; replay returns the receipt.
- Lost result submission: the encrypted agent outbox retries until acknowledged.
- Password result expires or is consumed: MC cannot recover it; a new reset
  safely invalidates the lost password.
- Duplicate email, invalid role/location, last-admin protection, ownership
  blockers, and state conflicts: no partial change occurs and MC shows the
  specific safe error.
- MC encryption or signing configuration unavailable: command creation fails
  closed with `capability_unavailable`.
- Unexpected customer exception: local details remain local; MC records only
  `internal_failure` and the command ID for support correlation.

## Verification

### Domain and persistence tests

- Password generation meets policy, uses a cryptographic source, and stores
  only a hash.
- Create, reset, disable, enable, and delete enforce legal transitions.
- Reset and disable revoke every session atomically.
- A one-time-password principal remains gated until self-service rotation.
- Duplicate emails, invalid roles/locations, ownership, and last-admin rules
  reject without partial writes.
- Safe deletion anonymizes identity while preserving the user ID and audit
  references.
- PostgreSQL and memory implementations have matching behavior.
- Customer receipts make replay idempotent and expire after retention.

### Transport and cryptography tests

- Fleet keys can pull and submit only for their bound deployment.
- Signature, contract, deployment, action, field allowlist, expiry, and replay
  failures are rejected before CLI execution.
- Command payloads and result private keys are sealed at MC rest.
- Results decrypt only with the matching per-command key and authenticated
  context.
- Lost responses return the identical encrypted receipt without repeating a
  mutation.
- Password reveal is atomic under concurrent requests and cannot be repeated.
- Expired secrets, snapshots, receipts, and outbox files are purged.
- Tests scan logs, API bodies, persistence fakes, and command arguments for
  password/hash leakage.

### API and UI tests

- Customer public routes contain no user-management or fleet operator API.
- Non-operator, non-admin, stale mutation sessions, and cross-deployment access
  fail closed.
- The MC page handles server selection, capability detection, refresh, forms,
  confirmations, polling, typed failures, and state-dependent actions.
- One-time reveal supports copy/dismiss without browser persistence.
- Switching deployment clears user and secret state.
- Offline and old-version servers display actionable, non-destructive states.

### End-to-end test

A fake host agent exercises MC job creation, authenticated pull, command
verification, customer execution, an intentionally lost first result response,
idempotent retry, MC completion, one-time reveal, forced user rotation, and
audit verification. The same harness covers directory refresh and every account
state transition.

## Acceptance criteria

1. An MC operator can select a compatible server and see its current human user
   directory without exposing that directory in heartbeats or plaintext MC
   storage.
2. The operator can create a user and reveal the generated initial password
   exactly once.
3. The operator can reset a password, immediately revoke sessions, reveal the
   replacement exactly once, and the user must rotate it at sign-in.
4. The operator can disable and re-enable an account with last-admin protection.
5. The operator can delete a disabled, unowned account; the login identity is
   irrecoverably anonymized while company content and audit continuity remain.
6. No command can cross deployment scope or execute after expiry, and retries
   cannot repeat a completed mutation.
7. No password, hash, user directory, or raw customer exception appears in
   heartbeats, plaintext persistence, URLs, logs, shell arguments, analytics,
   or browser storage.
8. Customer public route-denial and control-plane-isolation tests continue to
   pass.
