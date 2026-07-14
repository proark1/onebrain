# OneBrain Unified Platform Design

Date: 2026-07-07

## Summary

OneBrain becomes the single master database and governed AI data platform for
the current three products:

- OneBrain: central data, knowledge, memory, governance, retrieval, and admin.
- DarAI: personal/business assistant module and tool execution surface.
- Assaddar AI Communication: customer-service communication module for website
  chat, WhatsApp, Telegram, Meta channels, email, and telephone.

The end state is one canonical data model in OneBrain. Assistant and
communication services do not own long-term business, customer, personal, or
knowledge data. They run as modules installed on top of OneBrain.

The platform must support multiple sale and deployment packages:

- OneBrain only.
- OneBrain + personal assistant.
- OneBrain + communication service.
- Full suite: OneBrain + assistant + communication.
- Future modules such as CRM, booking, finance/admin automation, and internal
  employee helpdesk.

Railway is the first deployment target for speed. The architecture must remain
portable to dedicated or headless GDPR-compliant infrastructure later.

The engineering setup must support many customer deployments at once. Customer A
and customer B may each have their own OneBrain stack, installed modules, module
versions, database migration state, release channel, backups, and rollout
policy. Updates must be automated, version-controlled, observable, and
rollback-aware across the OneBrain core, assistant module, communication module,
workers, and UI.

Data privacy and security are release gates, not later hardening tasks. The
platform must be designed so real customer, employee, family, and private
assistant data can only be stored, retrieved, exported, deleted, or sent to AI
providers through explicit space, purpose, consent, retention, and audit
controls.

## Goals

- Move important data into one OneBrain database from the beginning.
- Avoid duplicate long-term tables across OneBrain, DarAI, and communication.
- Make privacy, security, and GDPR-grade governance the first implementation
  priority.
- Support private, business, customer service, shared, family, and project
  spaces inside the same platform.
- Make data boundaries explicit, auditable, and permission-aware.
- Let a business owner safely combine private assistant data and company data
  without leaking private data into customer service.
- Keep the UI minimal, visual, and easy to understand.
- Make module rollout simple for new customers.
- Keep current products useful while migrating them into the unified platform.
- Build the architecture so more services can be added over time.
- Support automated, version-controlled deployments for many customers.
- Provide an operator dashboard for customer instances, installed modules,
  versions, update status, health, backups, migrations, and rollout controls.

## Non-Goals For The First Implementation

- Do not build every DarAI feature in the OneBrain UI immediately.
- Do not build a public module marketplace in V1.
- Do not implement complex enterprise policy engines before the core space
  model works.
- Do not support non-Postgres storage as the primary database in V1.
- Do not remove all existing DarAI and communication database code before the
  new OneBrain contracts are proven.
- Do not process real customer/private data in production until the Phase 0
  privacy and security gates are implemented and verified.

## Privacy And Security Principles

OneBrain's business value depends on trust. The platform is only useful if
customers can understand and control where their data lives, which apps can use
it, and why.

### Privacy By Design

- Data is assigned to a space at creation time.
- Data is created with a purpose and source app.
- Apps receive only the minimum data needed for the requested purpose.
- Customer-service workflows cannot use personal, family, or private assistant
  data by default.
- Shared spaces must be explicitly enabled and visible in the UI.
- Raw customer/private data is not used to train shared models.
- Derived data, such as memories, summaries, embeddings, and entity links, keeps
  provenance back to the source record.

### Security By Default

- All service-to-service access uses scoped service identities, not shared root
  secrets.
- All secrets and provider credentials are encrypted at rest.
- Production cookies/tokens require secure settings and rotation plans.
- Every sensitive read/write emits an audit event.
- PostgreSQL RLS is enabled as defense in depth.
- The application still enforces explicit account, space, purpose, and role
  predicates before database access.
- Public surfaces are rate-limited and have payload size limits.
- Webhooks are verified with provider signatures or shared secrets.

### GDPR And EU Readiness

- Railway is acceptable for the prototype, but production customer data must be
  deployable to EU/GDPR-compliant infrastructure.
- Each deployment must document processors, regions, AI providers, and transfer
  mechanisms.
- Export, delete, retention, and consent records are core product features.
- Voice/call recording and AI handling need explicit notice and opt-out or
  handoff paths where legally required.
- Real data rollout requires signed processor agreements and a DPIA or
  equivalent privacy review.

### AI Data Handling

- AI prompts receive only retrieved, permission-approved context.
- Retrieval happens after account, space, purpose, consent, status, and
  classification checks.
- Embeddings are treated as derived personal/business data and included in
  deletion/retention flows.
- Sensitive spaces can require EU-sovereign model routing or refuse AI answers
  when no approved provider is configured.
- AI request logs must avoid raw secrets and minimize raw personal data.

## Core Product Model

### Accounts

An account is the billing and ownership container. Account types:

- `person`: private individual.
- `organization`: business/company.
- `family`: household/family group.
- `project`: optional project-specific container.

An account can own multiple spaces and app installations.

### Users

A user is a human login identity. A user can belong to many accounts and spaces.
For example, a founder can have:

- a personal account,
- a company account,
- a family account,
- a shared founder/business space.

### Spaces

Spaces are the main data boundary.

Initial space types:

- `personal`: private assistant data.
- `business`: company documents, policies, team knowledge, operational data.
- `customer_service`: customer conversations, channel inbox, approved support
  answers, call transcripts, handoff records.
- `shared`: intentionally shared private/business context for an owner or team.
- `family`: family members, shared reminders, household tasks, family notes.
- `project`: project-specific data, files, decisions, tasks, and memories.

Every important data row must include `account_id` and `space_id`.

### App Installations

Apps are modules installed for an account or space.

Initial apps:

- `onebrain_core`
- `assistant`
- `communication`
- `admin_console`
- `workers`

Future apps:

- `crm`
- `booking`
- `finance_admin`
- `internal_helpdesk`

An app installation records:

- enabled/disabled status,
- visible brand name,
- enabled spaces,
- allowed purposes,
- configuration,
- service identity,
- billing plan metadata.

### Purposes

Apps do not receive blanket data access. They request access by purpose.

Initial purposes:

- `assistant_context`
- `assistant_action`
- `customer_service_answer`
- `customer_service_inbox`
- `knowledge_management`
- `admin_management`
- `gdpr_export`
- `gdpr_delete`
- `analytics`
- `billing`

A customer-service answer purpose must never include personal or family data
unless explicitly linked through a shared space and approved for that purpose.

## Canonical Data Domains

### Governance

Tables:

- `accounts`
- `users`
- `organizations`
- `spaces`
- `memberships`
- `roles`
- `app_installations`
- `app_service_keys`
- `app_permissions`
- `consent_records`
- `retention_policies`
- `audit_logs`
- `data_access_events`

Responsibilities:

- login identity,
- account and space membership,
- role and purpose-based access,
- service key management,
- auditability,
- retention and GDPR workflows.

### Knowledge

Tables:

- `knowledge_sources`
- `knowledge_documents`
- `knowledge_chunks`
- `knowledge_suggestions`
- `document_ingestion_jobs`
- `document_files`
- `embedding_jobs`

Responsibilities:

- uploaded files,
- approved facts and FAQs,
- document extraction,
- chunking,
- embeddings,
- approval queue,
- retrieval.

This domain merges current OneBrain documents/chunks and the communication
platform's tenant knowledge.

### Memory And Knowledge Graph

Tables:

- `memories`
- `memory_provenance`
- `memory_redactions`
- `entities`
- `entity_mentions`
- `entity_relationships`

Responsibilities:

- personal assistant memory,
- business memory,
- project memory,
- source provenance,
- forget/delete workflows,
- entity-aware retrieval.

This domain migrates DarAI semantic memories, AI memory, episodic memory, and
knowledge graph entities into a OneBrain-owned model.

### People

Tables:

- `people`
- `contacts`
- `customers`
- `employees`
- `family_members`
- `person_identifiers`
- `relationships`

Responsibilities:

- unified people records,
- channel identifiers,
- emails and phone numbers,
- company relationships,
- family relationships,
- customer profiles.

The same human can be represented safely in multiple roles. For example, a
business owner can be a user, employee, contact, and family member without
duplicating identity facts.

### Communication

Tables:

- `channels`
- `channel_connections`
- `channel_credentials`
- `conversations`
- `conversation_participants`
- `messages`
- `message_deliveries`
- `calls`
- `call_transcripts`
- `handoff_requests`
- `inbox_items`
- `channel_webhook_events`

Responsibilities:

- website chat,
- WhatsApp,
- Telegram,
- Messenger,
- Instagram,
- telephone,
- email,
- delivery status,
- unified inbox,
- customer service history,
- handoffs.

This domain migrates the communication platform's tenant conversations, contact
profiles, channel adapters, calls, transcripts, deliveries, and audit records.

### Work And Assistant Data

Tables:

- `tasks`
- `events`
- `notes`
- `projects`
- `contracts`
- `reminders`
- `briefings`
- `assistant_actions`
- `assistant_action_approvals`
- `assistant_conversation_state`

Responsibilities:

- personal assistant workflows,
- business assistant workflows,
- task and calendar context,
- notes and contracts,
- approvals,
- undo/action trace.

DarAI becomes the assistant surface and tool executor, while these records move
to OneBrain.

### Integrations

Tables:

- `integration_connections`
- `integration_credentials`
- `integration_sync_jobs`
- `external_objects`
- `webhook_subscriptions`

Responsibilities:

- Gmail,
- Google/Microsoft calendar,
- Telegram bot,
- WhatsApp Cloud API,
- Meta channels,
- telephony providers,
- future CRM/accounting/booking systems.

Credentials are encrypted server-side and scoped to account, space, app, and
purpose.

### AI Operations

Tables:

- `ai_requests`
- `retrieval_traces`
- `model_usage_events`
- `prompt_versions`
- `evaluation_sets`
- `evaluation_runs`
- `cost_events`

Responsibilities:

- model usage tracking,
- answer traceability,
- retrieval debugging,
- prompt versioning,
- quality evaluation,
- cost controls.

### Deployment Control Plane

Tables:

- `customer_deployments`
- `deployment_environments`
- `deployment_services`
- `deployment_modules`
- `release_versions`
- `release_manifests`
- `module_versions`
- `module_compatibility`
- `rollout_policies`
- `rollout_runs`
- `migration_runs`
- `backup_runs`
- `health_check_runs`
- `deployment_incidents`
- `operator_audit_logs`

Responsibilities:

- track every customer instance,
- track enabled modules and installed versions,
- track target release versions and update availability,
- automate rollout waves,
- record database migration state,
- record backup and restore readiness,
- monitor health checks,
- expose rollback status,
- audit operator actions.

The control plane stores deployment metadata only. It must not store customer
documents, messages, memories, contacts, transcripts, or private/business
content.

## Access Control Rules

Every protected row has:

- `account_id`
- `space_id`
- `source_app`
- `classification`
- `status`
- `created_by`
- `purpose_visibility`

Access requires all checks:

1. Actor is authenticated as a user or service identity.
2. Actor belongs to the account or holds a scoped service key.
3. Actor has access to the target space.
4. Actor has the required role.
5. App installation is enabled.
6. Requested purpose is allowed for the app and space.
7. Data classification permits the action.
8. Record status is active/approved where required.
9. Retention and consent rules allow the action.

The database should use PostgreSQL row-level security as defense in depth.
Application code still applies explicit predicates and permission checks.

## Privacy And Security Controls

### Required Platform Controls

- Strong authentication for human users.
- Passwords hashed with a modern password hashing function.
- Signed, secure, rotating sessions.
- Scoped service keys for apps and workers.
- Service keys store only hashed secrets.
- Secrets encrypted with AES-GCM or a KMS-backed envelope provider.
- Per-space and per-purpose authorization checks.
- RLS policies for all tenant/account-scoped tables.
- Audit events for sensitive reads, writes, exports, deletions, consent changes,
  credential changes, and permission changes.
- Retention workers for conversations, calls, raw uploads, memories, and
  derived data.
- Data export and delete flows that include derived memories, embeddings,
  transcripts, and entity graph records.
- Request IDs and structured logs with secret redaction.
- Rate limits for public endpoints and app/service endpoints.
- Provider webhook signature verification.

### Privacy UI Requirements

The UI must make privacy understandable:

- Each data detail page shows its space, source app, purpose visibility, and
  who can use it.
- Each app installation shows which spaces and purposes it can access.
- Shared spaces show a clear warning that data can cross private/business
  boundaries.
- The privacy center exposes export, delete, consent, retention, and audit.
- The user can see why a memory exists and where it came from.
- The user can forget/delete a memory or source record.

### Release Gates

No module can be marked production-ready until:

- forbidden cross-space retrieval tests pass,
- service-key scope tests pass,
- audit tests pass,
- export/delete tests pass,
- retention tests pass,
- webhook verification tests pass where applicable,
- secrets are not stored or logged in plaintext,
- production config refuses unsafe defaults.

## OneBrain API Surface

### Core

- `POST /api/accounts`
- `GET /api/accounts/:accountId`
- `POST /api/accounts/:accountId/spaces`
- `GET /api/accounts/:accountId/spaces`
- `POST /api/spaces/:spaceId/members`
- `GET /api/spaces/:spaceId/access`

### App Installations

- `GET /api/accounts/:accountId/apps`
- `POST /api/accounts/:accountId/apps/:appId/install`
- `PATCH /api/accounts/:accountId/apps/:appInstallationId`
- `POST /api/app-service-keys`
- `DELETE /api/app-service-keys/:keyId`

### Knowledge

- `POST /api/spaces/:spaceId/knowledge/documents`
- `GET /api/spaces/:spaceId/knowledge/documents`
- `POST /api/spaces/:spaceId/knowledge/search`
- `POST /api/spaces/:spaceId/knowledge/ask`
- `GET /api/spaces/:spaceId/knowledge/suggestions`
- `POST /api/knowledge/suggestions/:suggestionId/approve`
- `POST /api/knowledge/suggestions/:suggestionId/reject`

### Memory

- `POST /api/spaces/:spaceId/memories`
- `POST /api/spaces/:spaceId/memories/search`
- `GET /api/spaces/:spaceId/entities`
- `POST /api/memories/:memoryId/forget`

### Communication

- `POST /api/spaces/:spaceId/conversations`
- `POST /api/conversations/:conversationId/messages`
- `GET /api/spaces/:spaceId/inbox`
- `POST /api/channels/webhooks/:provider`
- `POST /api/conversations/:conversationId/handoff`

### Assistant

- `POST /api/assistant/context`
- `POST /api/assistant/actions`
- `POST /api/assistant/actions/:actionId/approve`
- `POST /api/assistant/actions/:actionId/reject`

### GDPR

- `GET /api/accounts/:accountId/export`
- `GET /api/spaces/:spaceId/export`
- `DELETE /api/people/:personId`
- `DELETE /api/accounts/:accountId`
- `GET /api/audit`

## Module Responsibilities

### OneBrain Core

Owns:

- database,
- spaces and permissions,
- retrieval,
- knowledge ingestion,
- memory,
- people,
- audit,
- GDPR,
- admin API.

### Assistant Module

Owns:

- chat and voice assistant UX,
- tool execution,
- proactive suggestions,
- briefings,
- user-facing assistant settings.

Does not own:

- canonical memory,
- contacts,
- tasks,
- events,
- notes,
- contracts,
- long-term conversations.

### Communication Module

Owns:

- provider adapters,
- webhooks,
- widget,
- voice bridge,
- outbound delivery,
- channel compliance.

Does not own:

- tenant/customer database,
- approved knowledge,
- canonical conversations,
- customer profiles,
- call transcripts.

### Admin Console

Owns:

- minimal OneBrain UI,
- space switcher,
- data map,
- access visualization,
- knowledge approval,
- inbox,
- integration settings,
- GDPR tools.

## Minimal UI/UX Design

The UI should feel like a simple control center, not a dense enterprise suite.

Top-level navigation:

- Spaces
- Data
- Inbox
- Assistant
- Knowledge
- Integrations
- Privacy
- Settings

Core UX patterns:

- Space switcher is always visible.
- Every page shows which space is active.
- Every data detail page shows origin, access, usage, and delete/export options.
- Visual data map shows documents, people, conversations, memories, and apps.
- Access view answers: "Who can use this data and why?"
- Approval queues are simple: approve, edit and approve, reject, archive.
- Empty states guide setup without marketing copy.

Important screens for V1:

- Spaces overview.
- Space detail with enabled apps and data counts.
- Knowledge/document library.
- Knowledge approval queue.
- Unified inbox.
- People/contact detail.
- Assistant context viewer.
- Integrations setup.
- Privacy center with export/delete/audit.

## Railway Deployment Shape

Railway services:

- `onebrain-api`
- `onebrain-db`
- `onebrain-admin-ui`
- `onebrain-workers`
- `assistant-service`
- `communication-api`
- `communication-widget`
- `communication-voice`
- `communication-workers`
- optional `redis`

Only `onebrain-db` is the master database.

Each customer deployment is a data plane. The first supported deployment shape
is a dedicated Railway project/environment per customer or per serious test
customer:

- isolated database,
- isolated service variables,
- isolated secrets,
- isolated module set,
- isolated backups,
- isolated update policy.

The platform may later support shared multi-tenant SaaS deployments, but the
release and versioning architecture must work for both dedicated instances and
shared clusters.

Deployment presets:

- `brain_only`: core API, DB, admin UI, workers.
- `brain_assistant`: brain-only plus assistant service.
- `brain_communication`: brain-only plus communication API/widget/voice.
- `full_suite`: all services.

Branding config should live in account/app installation settings:

- product/customer-facing name,
- logo,
- colors,
- default language,
- email sender,
- widget theme,
- assistant name.

## Engineering And Release Architecture

### Control Plane And Data Planes

The platform has two operational layers:

1. **Control plane**
   - operator dashboard,
   - release catalog,
   - customer deployment registry,
   - module/version registry,
   - rollout orchestration,
   - health/backups/migration status,
   - operator audit logs.

2. **Customer data planes**
   - one or more customer OneBrain stacks,
   - OneBrain database,
   - installed assistant/communication modules,
   - customer-specific secrets,
   - customer data and backups.

The control plane can trigger deployment actions, but it must not hold customer
content. It may store customer name, deployment IDs, environment URLs, module
status, version numbers, health states, backup metadata, and rollout events.

### Release Manifest

Every release must have a version-controlled manifest. The manifest records:

- release version,
- Git commit SHA,
- container image tags or build artifacts,
- OneBrain core version,
- admin UI version,
- assistant module version,
- communication module version,
- worker version,
- database migration range,
- required environment variables,
- module compatibility matrix,
- breaking changes,
- security notes,
- migration plan,
- rollback plan,
- smoke test plan.

Example release bundle:

```text
onebrain-suite 2026.07.1
  onebrain-api: 0.8.0
  onebrain-admin-ui: 0.8.0
  onebrain-workers: 0.8.0
  assistant-service: 0.5.0
  communication-api: 0.6.0
  communication-widget: 0.6.0
  communication-voice: 0.4.0
  database: migrations 0042..0048
```

### CI/CD Pipeline

Every code change should move through automated gates:

1. Lint, typecheck, and unit tests.
2. Security checks for dependencies and secret leaks.
3. Database migration checks.
4. Cross-module contract tests.
5. Build container images/artifacts.
6. Generate release manifest.
7. Deploy to internal staging.
8. Run smoke tests.
9. Run privacy/security release gates.
10. Promote to rollout rings.

### Rollout Rings

Updates should not go to every customer at once.

Default rollout rings:

- `internal`: your own test deployment.
- `pilot`: selected friendly customer/test customer.
- `early`: low-risk customers.
- `stable`: default customers.
- `manual`: customers that require explicit approval or maintenance windows.

Each customer deployment has an update policy:

- automatic patch updates,
- automatic minor updates after pilot success,
- manual major updates,
- maintenance window,
- rollback preference,
- backup requirement,
- notification contacts.

### Database Migration Rules

Database migrations are the highest-risk part of multi-customer updates.

Rules:

- migrations are versioned and tracked per customer deployment,
- migrations are idempotent where possible,
- every migration has a dry-run/check mode where practical,
- destructive migrations require an explicit release gate,
- backup runs before schema-changing production updates,
- migrations use expand/contract patterns for breaking schema changes,
- old and new services must overlap safely during rolling updates,
- migration failures stop rollout for that customer and open an incident.

### Backups And Rollbacks

Each customer deployment needs:

- scheduled database backups,
- backup success/failure visibility,
- pre-update backup requirement,
- restore procedure,
- rollback plan per release,
- rollback compatibility notes for migrations,
- incident timeline.

Rollback options:

- service rollback to previous image/artifact,
- feature flag disablement,
- module disablement,
- database restore for severe migration/data corruption incidents.

### Feature Flags

Feature flags should control risky or staged features:

- per deployment,
- per account,
- per space,
- per app installation,
- per rollout ring.

Feature flags must be visible in the operator dashboard and audited when
changed.

### Operator Dashboard

The operator dashboard is for your team, not the end customer. It shows:

- customers/deployments,
- deployment type and region,
- enabled modules,
- current versions,
- available updates,
- rollout ring,
- health status,
- migration status,
- backup status,
- last deploy result,
- incidents,
- update/rollback buttons,
- maintenance window,
- customer contacts,
- operator audit log.

The dashboard must be minimal and operational:

- green/yellow/red status,
- "update available",
- "safe to update",
- "blocked by migration/privacy/test failure",
- one-click deploy to selected ring,
- one-click pause rollout,
- rollback action with confirmation.

It must not show customer content unless the operator also enters that
customer's normal OneBrain admin flow with audited, permissioned access.

## Migration Strategy

The target is one big OneBrain database. The migration is still phased to reduce
risk.

### Phase 0: Privacy And Security Foundation

Before migrating product domains, add the controls that everything else depends
on:

- threat model for OneBrain core, assistant, communication, workers, and public
  webhooks,
- account/space/purpose authorization contract,
- production-safe auth/session defaults,
- scoped service-key model,
- encrypted credential storage,
- audit event model,
- consent and retention model,
- data classification labels,
- RLS strategy and enforcement check,
- export/delete scope model,
- provider and processor register,
- real-data rollout checklist.
- release security gate checklist,
- operator access model for the control plane.

Success:

- unsafe production defaults fail startup,
- a service key can access only its allowed account, space, app, and purpose,
- customer-service purpose cannot retrieve personal/family data,
- shared-space use is explicit and audited,
- export/delete design covers source and derived data,
- real-data deployments have a documented privacy checklist,
- operator dashboard cannot access customer content by default.

### Phase 1: Foundation Schema

Add canonical tables for:

- accounts,
- organizations,
- spaces,
- memberships,
- app installations,
- app permissions,
- consent,
- retention,
- audit,
- data access events,
- encrypted credential metadata.

Success:

- create a business account,
- create personal/business/customer-service/family spaces,
- install assistant and communication modules,
- verify service keys are space and purpose scoped,
- verify audit, consent, retention, and export/delete paths exist for the new
  account and spaces.

### Phase 2: Knowledge Unification

Merge:

- existing OneBrain chunks/documents,
- communication knowledge sources/documents/chunks,
- FAQ/onboarding/suggestion flow.

Success:

- communication answers retrieve from OneBrain knowledge,
- pending knowledge is not answerable,
- approved knowledge works across modules.

### Phase 3: Communication Unification

Move communication-owned data into OneBrain:

- tenants to accounts/organizations/spaces,
- contacts/customers to people domain,
- conversations/messages/calls/transcripts to communication domain,
- channel connections and deliveries to integration/communication domain.

Success:

- website chat and at least one social/phone path write directly to OneBrain,
- inbox reads from OneBrain,
- customer export/delete works from OneBrain.

### Phase 4: Assistant Unification

Move DarAI-owned assistant data into OneBrain:

- semantic memories,
- AI memory,
- KG entities,
- notes,
- contacts,
- tasks,
- events,
- contracts,
- assistant actions.

Success:

- assistant context is fetched from OneBrain,
- assistant writes new memory/actions to OneBrain,
- personal/business/shared space boundaries are respected.

### Phase 5: UI Consolidation

Build the minimal OneBrain admin console:

- spaces,
- data map,
- access view,
- knowledge,
- inbox,
- assistant context,
- integrations,
- privacy.

Success:

- a customer can understand what data exists and which apps can use it,
- admin can approve knowledge,
- admin can export/delete scoped data.

### Phase 6: Deployment Templates

Create the control plane, Railway environment presets, and seed/setup scripts:

- brain only,
- brain + assistant,
- brain + communication,
- full suite.

Success:

- new customer rollout can be created predictably from a preset,
- brand names and module availability are config-driven,
- operator dashboard lists customer deployments and module versions,
- a release manifest can update an internal deployment automatically,
- migration and backup state are visible per deployment.

### Phase 7: Operational Hardening

Add:

- production RLS enforcement checks,
- retention workers for every domain,
- audit completeness dashboards,
- export/delete coverage for every domain,
- secret rotation workflows,
- AI provider routing,
- evaluation sets,
- cost limits,
- backup/restore procedures,
- incident response runbook,
- rollout rings,
- automated rollback path,
- release health dashboards.

Success:

- platform is ready for real customer data after legal/compliance setup and
  privacy/security release gates pass.

## First Build Milestone

The first implementation milestone should prove the unified platform with the
smallest useful vertical slice:

1. Create account and spaces.
2. Install assistant and communication modules.
3. Upload/seed approved knowledge into OneBrain.
4. Communication widget asks OneBrain and stores conversation/message in
   OneBrain.
5. Assistant asks OneBrain for context and stores a memory in OneBrain.
6. Privacy center shows app access, audit events, consent, retention, export,
   and delete controls.
7. Admin UI shows spaces, knowledge, inbox, app access, and audit events.
8. Operator dashboard shows the test customer deployment, installed modules,
   versions, health, backups, and migration state.
9. Railway deployment runs the full suite for one test customer with synthetic
   data until the real-data privacy checklist is complete.

## Testing Plan

Required tests:

- space access denies cross-space reads,
- customer-service purpose cannot read personal/family spaces,
- shared space can be used only when explicitly enabled,
- service keys are scoped to app installation, space, and purpose,
- production startup refuses weak secrets and unsafe cookie settings,
- credentials are encrypted and never returned in plaintext,
- provider webhooks reject missing or invalid signatures/secrets,
- pending knowledge is not retrievable,
- approved knowledge is retrievable across enabled modules,
- communication messages write to canonical OneBrain tables,
- assistant memories write to canonical OneBrain tables,
- GDPR export includes all selected account/space data,
- delete/forget cascades or redacts derived memories and entities,
- audit logs are written for sensitive reads and writes,
- retention workers remove or redact expired records and derived data,
- AI prompts include only permission-approved retrieved context,
- deployment presets enable only the expected modules,
- release manifests include all module/image/migration versions,
- incompatible module versions are rejected before deploy,
- migrations are tracked per customer deployment,
- failed migrations stop rollout and create an incident,
- pre-update backups are required for schema-changing releases,
- operator dashboard does not expose customer content,
- operator actions are audited,
- rollback path is tested for service releases.

## Risks And Mitigations

### Risk: Big merge breaks working products

Mitigation:

- migrate by domain,
- keep old code paths behind flags during transition,
- verify each vertical slice before removing old tables.

### Risk: Private/business data leakage

Mitigation:

- spaces from day one,
- purpose-based app permissions,
- RLS defense in depth,
- tests for forbidden cross-space retrieval,
- audit every sensitive read,
- real customer data blocked until privacy/security release gates pass.

### Risk: Secrets or provider tokens leak

Mitigation:

- encrypted credential storage,
- secret redaction in logs,
- no browser-visible service credentials,
- service-key hashing and rotation,
- incident response runbook.

### Risk: GDPR delete/export misses derived data

Mitigation:

- provenance for memories, summaries, embeddings, and entity links,
- export/delete tests per domain,
- retention workers include derived data,
- privacy center shows source and derived records together.

### Risk: UI becomes too complex

Mitigation:

- space-first navigation,
- simple data map,
- progressive disclosure,
- avoid exposing raw schema concepts to normal users,
- keep advanced controls in settings/privacy.

### Risk: Future modules create new silos

Mitigation:

- app installation model,
- canonical domains,
- app API contracts,
- no module gets its own master database for long-term platform data.

### Risk: Multi-customer updates become manual and error-prone

Mitigation:

- release manifests,
- deployment registry,
- rollout rings,
- automated health checks,
- migration state tracking,
- pre-update backups,
- operator dashboard,
- audited deploy/rollback actions.

### Risk: One customer update breaks other modules

Mitigation:

- module compatibility matrix,
- cross-module contract tests,
- staged rollout rings,
- feature flags,
- ability to pin a customer to a known-good version temporarily.

## Open Questions

- Which module should be the first live customer-facing proof: website chat,
  telephone, or assistant chat?
- Should assistant tasks/events be fully moved in Phase 4 or initially synced
  from external calendars/task tools?
- Which customer type should define the first default setup: gym, agency,
  local service business, or owner/operator solo business?
- Which AI providers are acceptable for real EU customer data in the first paid
  deployment?
- What is the first real-data hosting target after Railway prototype: Railway EU
  region with contractual setup, dedicated EU VM, managed EU Postgres, or
  customer-owned infrastructure?
- Should the first production customer shape be dedicated Railway project per
  customer, shared multi-tenant stack, or both with dedicated as the default for
  high-trust customers?
- Which release cadence should be the default: weekly stable releases plus
  emergency hotfixes, or continuous deployment through rollout rings?

## Approval Criteria

This design is ready for implementation planning when:

- OneBrain as the single master database is approved.
- The initial space types are approved.
- The initial modules and deployment presets are approved.
- The first build milestone is approved.
- Phase 0 privacy and security gates are approved.
- The real-data rollout checklist is approved.
- Multi-customer release/versioning architecture is approved.
- Operator dashboard scope is approved.
- Open questions have been answered or explicitly deferred.
