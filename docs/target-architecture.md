# OneBrain target architecture

```
status:      approved v2
date:        2026-07-11
supersedes:  v1 (approved target architecture, 15 sections)

Changed from v1:
1.  Tenancy is tiered: pooled multi-tenant OneBrain is the default; dedicated
    per-customer deployment is a priced enterprise tier, described honestly.
2.  One shared managed IdP with first-class organizations replaces
    realm-per-customer; BYO IdP (Entra/SAML) federates per organization.
3.  OneBrain is the sole OIDC relying party. Modules never touch IdP tokens;
    they verify short-lived OneBrain entitlement tokens locally.
4.  Sessions are revocable: refresh tokens re-checked against membership,
    a first-class revoke-all API, and an honest offboarding SLA.
5.  Background work runs under durable, revocable acting-for grants that are
    credentials, not just database rows.
6.  The runtime authorization formula is slimmed to four terms; purpose is an
    install-time module scope; ownership is personal-space membership.
7.  New sections: AI trust boundary, webhook ingress, platform-operator root
    of trust, LLM provider data flows, deletion/retention, conformance.
8.  Retrieval and object storage get an explicit enforcement contract; audit
    is honestly "tamper-evident and suppression-detectable", not "immutable".
9.  German-market defaults are built in: HGB/AO retention, BDSG §26 access
    governance, works-council-aware logging.
10. Migration sequencing is rewritten; per-customer provisioning moves last.
```

Each customer company runs on OneBrain with individual employee identities and
one login across all purchased modules.

```
Customer company
│
├── Shared managed identity provider (one organization per customer)
│   └── Individual employee authentication
│
├── OneBrain (pooled by default; dedicated on the enterprise tier)
│   ├── Company account
│   ├── Users and memberships
│   ├── Roles and permissions
│   ├── Spaces and classifications
│   ├── Central authorization and sessions
│   ├── Delegation grants
│   └── Canonical data, retention and audit history
│
├── AI Communication, if purchased
├── PersonalAssistant, if purchased
└── Future modules
```

## 1. Tenancy and customer isolation

Tenancy is tiered.

**Default tier: pooled multi-tenant OneBrain.** All customers share one
deployment, isolated by:

* `account_id` on every row and every index entry
* Forced PostgreSQL row-level security (no bypass role on the application DSN)
* Per-account data-encryption keys, envelope-wrapped by a platform KMS root
* Purpose- and space-scoped service keys per module, pinned to one account
* Per-account audit streams and export/erasure scopes

Honest costs of pooling, stated up front:

* A cross-tenant application bug is a cross-customer incident; such bugs are
  always fixed with top priority.
* Database and queue capacity are shared; a noisy tenant can degrade others.
* Some buyers will not accept pooling — that is what the dedicated tier is for.

The pooled tier launches with OneBrain core and PersonalAssistant. AI
Communication joins the pooled tier only after it is fully tenant-aware; until
then it runs alongside, scoped by its account-pinned service keys.

**Enterprise tier: dedicated deployment** ("Ihre eigene Instanz"), sold as a
priced option. The isolation unit is one Railway project per customer.
Named honestly, that provides:

* Project-scoped private networking: Customer A's services have no
  service-to-service network path to Customer B's
* A separate database, object namespace, secrets and module deployments

It does **not** provide:

* Egress control (any service can call out anywhere)
* Dedicated ingress or IP addresses (shared `*.up.railway.app` edge)
* Protection against platform-operator credentials; the operator control
  plane can reach every project (see section 16)

True network isolation (own VPC, egress filtering, dedicated ingress) is a
further tier that must be priced to fund it.

The control plane may know that accounts and deployments exist and whether
they are healthy. It must not contain customer conversations, documents,
emails or other business content — with exactly one carve-out: the webhook
ingress transit path defined in section 15.

**Fleet preconditions.** Before customer #3 on either tier, these are
mandatory, not aspirational:

* Pinned release bundles (section 20), never deploy-from-current-checkout
* Database migrations decoupled from application startup, run as a gated step
* Automated backup verification and periodic restores (section 18)
* Per-deployment (dedicated) and per-tenant (pooled) health alerting

## 2. Individual employee identities and the identity provider

Every employee receives their own identity:

```
IdP user ID (stable)
IdP organization → bound to company account
OneBrain user ID
Role assignments
Module assignments
Allowed spaces
Classification ceiling
Status
```

Employees do not share passwords.

**One shared managed IdP with first-class organizations** (Zitadel, or
Keycloak with Organizations) serves all customers. EU residency is a hard
selection requirement. The IdP handles:

* Passwords, MFA and passkeys
* Account recovery, email verification, brute-force protection
* Login sessions and signed identity tokens

Tenant identity is asserted as a **signed organization claim** in every IdP
token. OneBrain binds each organization to exactly one company account and
rejects any token whose organization claim does not match the target account.
"Tokens from another customer are rejected" is thereby a cryptographic check
with one configuration surface — one MFA policy, one email pipeline, one
recovery procedure — instead of N drifting realms.

**Bring-your-own IdP:** a customer's Entra ID or SAML provider is federated
into their organization. Their directory authenticates; our IdP issues the
token; OneBrain sees only standard OIDC. Nothing in section 3 changes.

**Dedicated-tier option:** enterprise customers may purchase their own realm
or issuer on the dedicated tier.

OneBrain stores only the IdP's stable user ID and the employee's authorization
data. In the target state OneBrain neither stores nor verifies passwords;
today it still does, and retiring that is a named migration step (section 21).

## 3. Single sign-on and sessions

**OneBrain is the sole OIDC relying party** (backend-for-frontend pattern).
There is exactly one OIDC implementation to configure, audit and patch.
Modules never see, verify or forward IdP tokens.

1. The employee opens any purchased module.
2. The module redirects to OneBrain's login endpoint.
3. OneBrain runs the OIDC flow with the shared IdP; the IdP verifies
   credentials and MFA and returns an identity token with the org claim.
4. OneBrain verifies issuer, signature, audience, expiry and the
   organization-to-account binding.
5. OneBrain resolves membership, roles, spaces and clearance, and issues the
   module two artifacts:
   * an **entitlement token**: short-lived (5–15 minutes), audience-scoped to
     that module, signed, carrying a snapshot of roles, allowed spaces and
     clearance — verified locally by the module on every request, no
     per-request call home;
   * a **refresh token**: opaque, stored server-side in OneBrain's session
     store, revocable individually. Every refresh re-checks membership status
     and roles; a deactivated or downgraded user gets no new entitlement token.
6. Every canonical-data request is still authorized by OneBrain per request
   (section 6). Module-local reads rely on the entitlement token.

Since OneBrain is already on the path of every data request, making it the
login authority adds no new single point of failure.

**Revocation** is first-class: a `revoke all sessions for user X` API deletes
the user's refresh tokens and personal access tokens in one call. Offboarding
invokes it (section 4).

**Honest offboarding SLA:** access to canonical data dies immediately (the
per-request decision consults live membership). Module-local access dies
within one entitlement-token TTL — at most 15 minutes. We state this number
to customers rather than promising "instant".

**Non-browser access:**

* Native and CLI clients use Authorization Code + PKCE against OneBrain.
* Long-running integrations use OneBrain-issued personal access tokens:
  scoped, individually revocable, held in the same session store. No module
  mints its own long-lived credentials.

**Transitional shim:** the existing assistant identity handoff endpoint
(`POST /api/service/assistant/identity/login`), in which a module forwards an
employee's password to OneBrain over a service-key call, is a transitional
shim. No further module may integrate against it. It is retired at migration
step 4 (section 21).

## 4. Company, users and memberships

One company account contains multiple individual users.

```
Company
├── Owner
├── Managers
├── Support employees
└── General employees
```

Onboarding creates the first Owner **and requires a second Owner or a named
recovery contact** (see section 16 for why). After that:

* Owners invite employees and assign roles and module access.
* Owners may delegate `users.manage`.
* Platform operators do not automatically become customer users.

Membership deactivation can be driven three ways: Owner action, an IdP event
(back-channel logout / account disable), or **SCIM from the customer's own
directory** — so "we disabled her in Entra" is enough to remove her here.

When an employee leaves:

* Their IdP account is disabled (directly or via SCIM/IdP event).
* OneBrain calls revoke-all-sessions: refresh tokens and PATs are invalidated;
  entitlement tokens expire within one TTL.
* OneBrain membership becomes inactive; every subsequent canonical-data
  request is denied immediately.
* All acting-for grants naming that employee are revoked (section 11).
* Background jobs created under that employee are reviewed or cancelled.
* Business records are reassigned or retained per policy.
* Their personal spaces follow the two-tier regime of sections 9 and 18.

## 5. Roles and permissions

Four **code-defined role templates** ship with the platform:

* Owner
* Manager
* Support
* Employee

Roles are collections of permissions over a permission grammar:

```
users.read            users.manage
roles.read            customer_data.read     customer_data.write
management_data.read  company_documents.read
personal_space.read_own
audit.read            data.export            data.delete
break_glass.request   break_glass.approve
retention.manage
```

The default rule is deny. Access exists only when explicitly granted.

A customer-defined role editor and a delegation lattice ("managers cannot
grant permissions they do not possess") are deliberately deferred until a
paying customer needs them. Until then, a new role is a same-week code change
on top of the same grammar — customization stays additive, and we do not
maintain a permission-granting engine that no customer exercises.

## 6. The authorization decision

One engine decides everything. Every route — human or service — calls a
single `decide(principal, action, resource)` function. There are no parallel
part-engines with their own interpretations.

The runtime intersection for a human-attributed request is four terms:

```
Human permission
∩ company account
∩ allowed space
∩ classification clearance
= effective access
```

Two former terms are handled elsewhere, deliberately:

* **Purpose** is an install-time module credential scope. Each module's
  installation fixes its allowed spaces and purposes; every service request
  declares a purpose, which is checked against that installed allowlist as a
  scope cap (like an OAuth scope) and **logged on every request** for audit.
  It is not a per-human, per-record authorization dimension. Out-of-pattern
  purpose usage feeds the anomaly alerting in section 13.
* **Ownership** is expressed solely as personal-space membership. There is no
  separate ownership field to drift out of sync with space membership.

Example: a support employee holds `customer_data.read`. That still does not
allow:

* A management space (space term fails)
* Another employee's personal space (space term fails)
* Restricted governance records (classification term fails)
* The same data requested through a module not installed for that space or
  purpose (module scope cap fails)

Every denial carries an **enumerated deny reason** written to the audit
stream, and an operator-only **"explain access" endpoint** answers "why can't
user X see record Y" from the same engine — one support ticket, one query,
instead of reverse-engineering a multi-term intersection by hand.

PostgreSQL row-level security mirrors the account and space restrictions as a
second, independent layer.

## 7. Spaces

Spaces separate data by purpose and audience without extra logins.

```
company-shared
customer-service
management
restricted-governance
personal/{employee-id}/work-correspondence
personal/{employee-id}/assistant-private
```

Examples:

* AI Communication normally reads and writes `customer-service`.
* Managers access `management` and relevant shared spaces.
* PersonalAssistant accesses the employee's own personal spaces.
* Company policies live in `company-shared`.
* Sensitive governance records live in `restricted-governance`, which is what
  makes them Restricted (section 8).

A module receives explicit installation permission for every space and
purpose it uses; that installation is the scope cap of section 6.

## 8. Data classification

The schema carries a four-level enum: Public, Internal, Confidential,
Restricted. **v1 operates two levels**, and classification is
**auto-assigned by intake source and destination space — never by per-record
human labeling**:

* Content intended for customers (chatbot knowledge, published policies) is
  **Public** after explicit approval. Uploading into a customer-facing
  knowledge space *is* the approval act; the publication lifecycle
  (pending → approved) enforces that nothing unapproved is ever retrievable.
* Everything else is **Internal** by default.
* **Restricted** is assigned by space: records in `restricted-governance` are
  Restricted because of where they live, not because someone picked a label.
* Confidential remains in the enum for later differentiation.

A gym owner never sees a classification picker. The owner uploads the
opening-hours PDF into the chatbot's knowledge space; it becomes retrievable
to customers on approval; nothing to mislabel, nothing to debug.

Classification is preserved through derived data, exports and search indexes,
and each level carries an **inference-eligibility ceiling** — which levels may
enter model prompts at all (section 17). Unknown labels still parse fail-closed
to Restricted.

## 9. Personal spaces: two tiers

The blanket promise "employee email is private from the employer" is one the
product cannot keep for company mailboxes, so personal data is split honestly:

**Tier 1 — work correspondence of the employee**
(`personal/{id}/work-correspondence`): the company-provided mailbox and
calendar. This is company data that happens to be per-employee — in-flight
customer negotiations live here. Governance is **audited and notice-based**,
not secret and not break-glass: sick leave and departures are routine business
continuity, handled by normal, logged, employee-visible access. A
provisioning-time toggle records whether private use of company accounts is
forbidden or permitted, because German employer-access law turns on exactly
that distinction (BDSG §26 requires a documented lawful basis either way).

**Tier 2 — assistant-private** (`personal/{id}/assistant-private`): draft
replies, personal notes, personally connected accounts, assistant
conversations. Fully private by default. A manager or Owner sees none of it.
Access requires break-glass (section 10), no exceptions.

Management always sees company-level operational metadata — whether
PersonalAssistant is enabled, whether a provider connection is healthy —
without content.

Works-council note: per-employee read and activity logging is itself subject
to co-determination under §87(1)(6) BetrVG for customers with a Betriebsrat.
The audit design (section 13) logs at query granularity by default partly for
this reason, and the two-tier model above is the description we hand a works
council.

## 10. Break-glass access

Exceptional access to an assistant-private space requires all of:

* The dedicated `break_glass.request` permission
* A documented reason
* MFA step-up, specified concretely: OneBrain requires a fresh IdP
  authentication (OIDC `max_age`) and checks the `acr` claim for the required
  factor — not an unspecified "fresh MFA"
* A narrowly defined target (one employee, named spaces, named record types)
* A short expiration
* Tamper-evident audit of the request, the approval and **every record read**
  (section 13, category c)
* Employee notification

**Small-organization mode.** In a five-person company the Owner is requester
and approver; a second independent approver is fiction. Where no independent
approver exists:

* Break-glass requires **pre-access notification** to the affected employee
  with an activation delay before access opens.
* The delay is suppressible only by a documented legal-hold reason, which is
  itself audited.
* After expiry, an **automatic post-hoc report** lists exactly which records
  were read, delivered to the employee and retained in audit.

Notification-with-delay is the preventive control that replaces the missing
second approver; the audit trail alone is only detective. Break-glass access
never silently becomes standing access.

## 11. Machine identities and delegation

Every module has its own machine identity (service key):

```
Communication service identity
PersonalAssistant service identity
OneBrain worker identity
Backup/maintenance identity
```

Service keys are pinned to one tenant and one account, scoped to installed
spaces and purposes, and carry a **hard Public clearance ceiling** with no
role. That ceiling is the invariant that lets an untrusted adapter talk to the
platform; it is never raised globally. It is relaxed only per-request, through
one of two employee-bound mechanisms:

**Interactive requests** carry the employee's entitlement token (section 3)
alongside the service key. The token is the actor claim; OneBrain evaluates
the intersection of the employee's rights and the module's scopes. On any
personal-space access the employee-bound token is **required** — the service
key alone is never sufficient.

**Background work** — PersonalAssistant's 3 a.m. provider sync, when no
session exists — runs under a durable **acting-for grant**:

```
Grant
├── employee_id
├── module
├── space list
├── purpose list
├── expiry
└── revoked_at
```

* Minted when the employee connects a provider; consent-backed; listed in the
  employee's settings; individually revocable.
* **A grant is a credential, not just a row**: per-grant secret material lives
  in the vault and must be presented alongside the service key (or the module
  credential is sender-constrained via mTLS/DPoP). A leaked service key alone
  exercises no grant.
* At call time, OneBrain evaluates the grant against the employee's
  **current** rights and clearance — role downgrades and offboarding apply
  immediately — capped by the grant's space and purpose lists.
* Every exercise is audited with employee, module, purpose and correlation ID.

The service key authenticates the module; the grant authorizes the
delegation. A compromised Communication credential still cannot touch
management or personal spaces: wrong scopes, no grant, Public ceiling.

## 12. Storage and retrieval enforcement

Storage inventory (per account on the pooled tier, per deployment on the
dedicated tier):

* Managed IdP: passwords, MFA, recovery
* OneBrain structured database: users, roles, spaces, memberships, grants,
  sessions, canonical records, retention policies, authorization metadata
* Encrypted object storage: documents, attachments, recordings
* Secret vault: OAuth tokens, per-grant secrets, per-account data keys
* Module operational databases: queues, retries, delivery state, outbox
* Search/vector index: derived, rebuildable retrieval data

OneBrain remains the canonical platform; module databases must not become
competing sources of truth. The outbox rule stands: a write is not
synchronized until OneBrain confirms it. Its symmetric twin is the deletion
tombstone (section 18).

**Retrieval enforcement contract** — "derived and rebuildable" is a durability
property; retrieval also needs an authorization property:

* Every index row carries `{account, space, classification, owner}` plus the
  space kind, so the filter compiles without a join.
* Human principals **always** query with their compiled allowed-space set,
  built from memberships. There is no unscoped human query: a set built from
  memberships excludes other employees' personal spaces by construction.
* On personal-kind spaces the filter adds an `owner == caller` clause as
  defense-in-depth against mis-tagged or space-less rows.
* The publication-status and classification clauses stay in the same compiled
  filter, enforced outside the language model, in both the in-memory predicate
  and the SQL WHERE clause.
* Module-local indexes implement this identical contract or delegate
  retrieval to OneBrain. No third option.
* Deletion tombstones reach every derived index, verified by periodic rebuild.

**Object storage** — where RLS does not exist:

* Every object read is brokered by OneBrain's section-6 decision, which then
  mints a **short-TTL, single-use signed URL** and writes the audit event.
  Large files are served by redirect, not proxied bytes.
* Modules never hold read-scoped bucket credentials.
* Object keys are `{account}/{space}/{id}`. Classification is **not** in the
  key — it is mutable metadata, and reclassification must not require object
  copies.

## 13. Audit

The honest property is **externally anchored, tamper-evident and
suppression-detectable** — not "immutable", which no self-hosted design can
deliver against its own operator. Two layers:

1. **In-database append-only trigger** on the audit table (blocks UPDATE and
   DELETE). First line of defense against application bugs.
2. **Dual-write via the existing outbox to an external write-once sink** —
   S3 Object Lock in compliance mode, in a separate cloud account outside the
   deployment's trust domain, with per-customer prefixes and keys. Events
   carry per-customer **hash chains and monotonic sequence numbers**, so a
   suppressed or drained write path shows up as a detectable gap, not silence.

Audited events: login/logout, MFA and recovery, invitations and membership
changes, role changes, module installations, service-key and grant lifecycle,
sensitive reads, exports and deletions, denials with enumerated reasons,
break-glass lifecycle, classification changes, background operations for a
user.

**Sensitive reads, defined** (a RAG answer touches hundreds of chunks;
auditing every one everywhere would make audit the largest table in the
system):

* **Per-record** audit for exactly three cases:
  (a) Restricted classification,
  (b) any non-self access to a personal space,
  (c) any read under an active break-glass session.
* **Query granularity** for everything else: actor, module, purpose, spaces,
  highest classification touched, record count, correlation ID. The
  correlation ID lets an investigation reconstruct a query's record set from
  retrieval logs.

**Bulk-exfiltration bounds:** `data.export` above a volume ceiling requires
Owner notification (or second approval where one exists), and the
metadata-only control plane runs a reads-per-hour anomaly alert per actor.
Authorized-but-anomalous is a detection problem, and detection is designed in,
not hoped for.

Audit records identify company, human actor, service actor, purpose, target,
decision, time and correlation ID — pseudonymous references and pointers,
never copied content (this is also what makes section 18's erasure compatible
with retained audit metadata under Art. 17(3)(b)/(e)).

## 14. AI trust boundary

The modules are AI agents that read untrusted third-party content — inbound
customer messages, emails, calendar invites — and act over private data.
Authorization cannot catch a prompt-injection attack, because every check
passes: the module is authorized, the employee is authorized, the space grant
is valid. This risk scales with inbound message volume, not customer count —
it is present at customer #1. So model context is a trust boundary with its
own rules:

* **Untrusted tagging at intake.** Third-party-originated content is tagged
  untrusted when it enters the system, and the tag survives into every derived
  artifact and retrieval result.
* **Output mediation, tiered by context provenance** — not by trigger, since
  every customer reply is customer-triggered:
  * A reply generated under untrusted influence may draw only on the
    originating conversation plus explicitly whitelisted knowledge spaces.
  * Any outbound message whose generation context pulled retrieval from
    beyond that set **escalates to draft-for-approval** instead of sending.
* **Approval is paired with sanitization.** Human approval alone fails against
  payloads hidden inside an approved draft (data encoded in link URLs, quoted
  text); outbound drafts get link and content sanitization before send.
* **Minimum-space retrieval.** Context injected into a prompt is scoped to the
  minimum spaces the task needs, never the module's full installed grant.
* The draft/approval workflows and human handover that already exist as
  product features in PersonalAssistant and AI Communication are **security
  invariants**, required in both modules, and may not be disabled per
  conversation by anything the model itself decides.

Example: a WhatsApp message tries to steer AI Communication into disclosing
another conversation. The reply context is confined to that conversation plus
the public knowledge space; the attempt to widen retrieval flips the reply to
a human-approved draft; the audit trail shows the escalation.

## 15. Webhook ingress

Inbound provider webhooks (Meta/WhatsApp callbacks, email notifications) enter
through a **thin shared ingress** — for all customers, on both tiers:

* Verifies the provider's signature at the edge (for Meta,
  `X-Hub-Signature-256` computed with the single vendor app's secret).
* Resolves routing **only from provider-verified identifiers** (the account
  IDs Meta asserts), never from payload-self-declared fields.
* Forwards immediately to the owning customer's stack and retains nothing
  beyond a correlation ID.

This design is forced, not chosen: Meta allows one webhook callback URL per
app, and the ISV path (Tech Provider model with Embedded Signup, one vendor
app for all customers) is the only operationally feasible one — per-customer
Meta app registration and review does not scale to this business.

Section 1's control-plane rule carves out exactly this transit exception: the
ingress momentarily handles customer message content in flight. It is
therefore a named shared trust point — minimal code, no persistence, its own
audit stream, reviewed as such.

## 16. Platform operator access and root of trust

Somebody provisions accounts, runs migrations, deploys code, restores backups
and rescues locked-out Owners. That somebody's credentials sit inside every
isolation boundary in this document, so they are architecture, not IT hygiene:

* **The deploy pipeline is a named machine identity** with deploy-only
  rights: per-environment deploy tokens, no workspace-wide token in CI, and no
  data-plane DSNs or customer secrets in pipeline scope.
* **Hardware-key MFA on the GitHub and Railway accounts** is a stated
  architectural control — those two accounts are the actual root of trust.
* **Supply chain:** pinned lockfiles with dependency-diff review in CI. A
  deploy-only identity does not stop malicious code from reaching customers at
  runtime; the diff review and the next control do.
* **Canary rollout:** every release lands first on the operator's own
  accounts, with a delayed fan-out window before external customers. (Solo
  operation makes self-approval gates theater; CI gates + canary + delay are
  the honest control set.)
* **Operator content access uses section 10's break-glass machinery** —
  documented reason, narrow target, expiry, tamper-evident audit. Customer
  notification is enforced from the first external customer onward. Routine
  support ("the assistant gives wrong answers") is done with metadata,
  explain-access (section 6) and the customer's consent — not silent psql.
* **Sole-Owner recovery is a first-class flow:** out-of-band identity
  verification, mandatory customer notification, an action that touches the
  identity realm but never customer data, fully audited. Onboarding requires
  a second Owner or recovery contact precisely so this flow stays rare.

Stated plainly: per-customer isolation protects customers from each other. It
does not protect anyone from a compromised operator; the controls above are
what does.

## 17. LLM provider data flows and EU residency

Every conversation, chunk and email the modules process is sent to a
third-party model API. That flow is part of the architecture, not an
implementation detail:

* LLM and embedding providers are **named subprocessors**, held in the
  processor registry with region and DPA status; the current subprocessor
  list is a **deliverable of provisioning** and of every DPA review.
* Inference and embedding endpoints must be **EU-region with zero-retention
  terms**. Model choice is constrained by this, and that is accepted.
* Each classification level has an **inference-eligibility ceiling**:
  Restricted content is redacted or excluded from AI features entirely;
  the ceiling is enforced in the same compiled filter as retrieval.
* Embedding computation runs inside OneBrain's boundary against the EU-region
  endpoint; no module sends content to a model provider directly.
* Stated plainly: content that enters a prompt crosses the deployment
  boundary by design. Isolation is "isolated except inference", and the DPA
  says so.
* All per-customer resources — IdP, database, objects, vault, backups,
  inference — are pinned to EU regions.

## 18. Deletion, retention and customer offboarding

**Tombstones are the platform primitive symmetric with the sync rule:** the
outbox says a write is not synchronized until OneBrain confirms it; likewise a
deletion is not complete until every consumer confirms the tombstone.

* OneBrain emits a tombstone for every erasure; modules and indexes must
  consume and confirm.
* **Hard confirmation required** for module operational databases and object
  storage.
* **Delete-or-rebuild acceptable** for rebuildable indexes.
* A **timeout and escalation path** ensures a dead module cannot block the
  Art. 17 clock: unconfirmed tombstones page the operator and are resolved by
  rebuild or manual attestation.

**Retention is a first-class object.** `RetentionPolicy` (per space, per
record type, with duration, action and legal basis) is canonical, extended
with a **legal-hold flag**, governed by `retention.manage`. Precedence is
fixed and enforced everywhere, including every erase endpoint:

```
legal hold  >  erasure request  >  retention expiry
```

An erase request that hits held or retention-bound records erases what it
lawfully can, reports what it lawfully cannot, and never silently deletes
through a hold.

**German defaults at provisioning:**

* Business correspondence and records relevant to §257 HGB / §147 AO: retain
  6 years (10 where the record class requires it) — exactly the material that
  flows through AI Communication, so "delete everything about customer X" must
  not delete invoice-relevant conversations inside the statutory window.
* Personal spaces: delete on departure plus a short grace period, per the
  two-tier rules of section 9.

A scheduled retention job enforces policies, emitting audit events and
tombstones. Audit metadata survives content erasure (pseudonymous references,
Art. 17(3)(b)/(e)).

**Customer offboarding** is a defined procedure, not an improvisation:

* Export bundle in documented formats
* Destruction of the customer's data keys (pooled tier: the per-account
  envelope keys; dedicated tier: deployment teardown plus key destruction)
* **Signed destruction attestation** per Art. 28(3)(g)
* A **documented backup-expiry window** in the DPA — backups age out on a
  stated schedule rather than pretending to be instantly erasable

**Restore drills:** backups are verified by periodic restores on a rotating
schedule. A backup that has never been restored is a hope, not a control.

## 19. Repository responsibilities

OneBrain owns:

* Company accounts, identity bindings (IdP org → account), memberships
* Roles and permissions; the single authorization engine and explain-access
* Sessions, entitlement tokens, refresh tokens, PATs, revocation
* Delegation grants and their secrets
* Spaces, classifications, retention policies, legal holds, tombstones
* Service identities and installations (space + purpose scopes)
* Canonical cross-application records; retrieval authorization contract
* Central audit contract and the external audit sink
* The webhook ingress and the object-read broker

AI Communication owns:

* Channel connectors; conversations and delivery operations
* Untrusted-content tagging at intake; output mediation and human handover
  as security invariants (section 14)
* Communication-specific queues; webhook processing behind the shared ingress
* Synchronization of canonical records into OneBrain, tombstone consumption

PersonalAssistant owns:

* Personal orchestration; provider synchronization under acting-for grants
* Draft/action workflows and approval execution as security invariants
* Scheduling and job processing; tombstone consumption
* Access only to the authenticated employee's personal spaces and explicitly
  shared company spaces

## 20. Contracts and conformance

Rules enforced in three codebases stay in agreement by construction, not by
memory:

* **Each customer stack ships as one pinned release bundle** — OneBrain and
  modules at versions tested together. There is no hotfixing one module in one
  stack without cutting a bundle; for a solo operator that discipline is a
  feature.
* **Every sync envelope and every token carries a contract-version field,
  enforced by rejection at handshake.** A version mismatch fails loudly at
  connect time instead of corrupting silently in production. The existing
  `assistant.v1` tag graduates from advisory to enforced, and versioning
  extends to the identity handshake and the Communication sync path.
* **A shared allow/deny fixture matrix** encodes the authorization semantics —
  including section 6's examples verbatim — and runs in all three
  repositories' CI **against real PostgreSQL with RLS enabled**. The mirrored
  RLS layer is proven where it runs, not skipped. With one developer,
  automated cross-repo conformance is the only reviewer this system will
  ever have.

## 21. Migration sequencing

The gap between this document and the current implementation, stated honestly
— including OneBrain's own gaps, not only the modules':

* OneBrain is itself today's password authority: it stores hashes and
  verifies logins, which section 2 forbids in the target state.
* OneBrain sessions are stateless 7-day HMAC tokens with no server-side
  store; the only revocation lever is whole-deployment secret rotation.
* There is no MFA anywhere, roles are hard-coded to one business, and human
  principals query retrieval without a compiled space set.
* AI Communication operates as an independent identity authority with its own
  password store and is not yet tenant-aware.
* PersonalAssistant lacks row-level security and employee/company-scoped
  provider queries, and its background sync has no delegation credential.
* The assistant identity handoff forwards plaintext passwords between
  services (the section-3 shim).
* No tombstone contract, no legal-hold flag, no external audit sink, no
  webhook ingress component, no conformance suite.

The order matters; the sequence is cheapest-risk-reduction first, cloning
last:

1. **Fix the Communication P0s and OneBrain retrieval space-scoping** — days
   of work. Compile every human principal's allowed-space set from
   memberships; add the owner clause on personal-kind spaces as
   defense-in-depth.
2. **Make OneBrain sessions revocable.** Postgres-backed session and
   refresh-token store (the pattern already proven in PersonalAssistant's
   session table), 5–15 minute entitlement tokens, membership re-check on
   refresh, the revoke-all API wired into offboarding.
3. **Define and enforce the sync/deletion contract:** contract-version fields
   with handshake rejection, tombstones with confirmation semantics, the
   shared fixture matrix in CI.
4. **Put the shared-organization IdP in front of OneBrain.** OneBrain becomes
   the sole OIDC relying party; MFA and passkeys arrive here; **both**
   password stores (OneBrain's and Communication's) are retired, and the
   password-forwarding shim is deleted.
5. **Per-customer provisioning last**, cloning a stack that is already
   correct — never before, which would clone every unfixed defect into N
   deployments. Pull this forward only if a signed customer contractually
   requires isolation first (plausible for German B2B buyers on residency
   grounds), accepting the documented cost that the identity migration then
   happens N times.

In parallel, gated by milestones rather than dates: delegation grants land
before PersonalAssistant background sync ships; Communication becomes
tenant-aware before joining the pooled tier; and the fleet preconditions of
section 1 — pinned bundles, startup-decoupled migrations, backup
verification, alerting — are complete before customer #3 on either tier.
