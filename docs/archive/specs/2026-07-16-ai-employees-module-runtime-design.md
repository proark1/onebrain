# AI Employees Module Runtime Design

**Date:** 2026-07-16

**Status:** Approved for implementation planning

**App ID:** `ai_employees`

**Contract version:** `ai_employees.v2`

## 1. Objective

Turn AI Employees into an optional, deployable OneBrain module that provides a
persistent, editable, productive team of governed AI employees. The module owns
the employee roster, character configuration, model routing, individual and
group conversations, missions, action proposals, connector bindings, approval
queues, and operational metrics.

The initial live test uses Gemini for all employees. The runtime must still be
provider-neutral from the first release so a project administrator can assign
other approved backends later, including a Claude-based technical agent runtime.

The first real external connector is Google Workspace Calendar. Internal
OneBrain actions such as reports, tasks, briefs, checklists, KPI analysis, and
action proposals are available without an external connector.

This design supersedes the eight-person roster in
`2026-07-16-ai-employee-action-governance-design.md`. The approval and action
safety rules from that design remain applicable unless this document states a
more specific rule.

## 2. Module boundary

AI Employees is one optional customer module, not a collection of always-on
features in OneBrain Core.

### 2.1 AI Employees owns

- the 16 default AI employee profiles and reporting hierarchy;
- administrator-edited character prompts and version history;
- model policies and provider selection per employee and task type;
- agent conversations, approved memories, missions, squads, and run state;
- structured multi-agent discussion and synthesis;
- internal AI work products and action proposals;
- connector capability bindings and the Google Calendar adapter;
- human approval queues for consequential actions;
- module dashboards, metrics, alerts, and audit views.

### 2.2 OneBrain Core provides

- authentication, sessions, project-administrator authorization, accounts, and
  spaces;
- app installation and purpose checks;
- Postgres/RLS persistence, scoped retrieval, classifications, and approved-data
  filtering;
- intake records, documents, KPIs, jobs, platform audit, privacy, retention, and
  legal-hold primitives;
- credential metadata and opaque secret references;
- the base LLM transport and EU-sovereign routing guard.

### 2.3 Deployment shape

The first release is a vertical module inside the existing OneBrain API, worker,
and admin UI images. It does not require a fourth core container. Every module
route and worker job must verify that the `ai_employees` app is installed and
active for the requested account and space.

The `onebrain_ai_employees` and `full_stack` provisioning bundles install the
module. Other bundles omit it. The default enabled spaces remain `business` and
`shared`; the module receives no automatic access to personal, family, customer
service, Mission Control, fleet, or provisioning data.

Disabling the module pauses schedules, blocks new agent runs and connector
execution, and preserves configuration and history for reactivation. Privacy
erasure and account deletion remove module data through the existing deletion
and tombstone contracts.

## 3. Default organization

The default installation contains 16 fictional European AI characters. Country,
age, and pronouns are presentation attributes. They are not claims of human
identity, citizenship, lived experience, or professional licensure.

### 3.1 Reporting structure

```text
Human founder / project administrator
  |
  +-- Clara Hoffmann — AI Chief of Staff
      |
      +-- Oliver Bennett — Corporate Strategy Manager
      |
      +-- Leadership Council
          +-- Élodie Martin — Chief Operating Officer
          +-- Lukas Schneider — Chief Product & Technology Officer
          +-- Antoine Dubois — Chief Marketing Officer
```

Clara chairs the four-person AI leadership council. Oliver directly reports to
Clara and advises the council but is not a council seat. The human project
administrator retains final authority.

Standing teams remain below six AI members:

- Chief of Staff office: 2;
- Leadership Council: 4;
- Operations & Corporate pod: 5;
- Product, Technology & Security pod: 4;
- Market & Customer pod: 5.

Any temporary mission squad contains at most six AI participants, including
Clara.

### 3.2 Character roster

| ID | Character | Age | Country | Pronouns | Role / reports to | Character traits |
| --- | --- | ---: | --- | --- | --- | --- |
| `chief_of_staff` | Clara Hoffmann | 44 | Germany | she/her | AI Chief of Staff | Composed, perceptive, decisive; warm and concise; may compress nuance for momentum. |
| `corporate_strategy_manager` | Oliver Bennett | 38 | United Kingdom | he/him | Corporate Strategy Manager / Clara | Analytical, curious, constructively skeptical; calm and evidence-first; may over-explore alternatives. |
| `chief_operating_officer` | Élodie Martin | 46 | France | she/her | Chief Operating Officer / leadership council | Disciplined, pragmatic, energetic; direct and structured; impatient with open-ended debate. |
| `operations_manager` | Felix Wagner | 41 | Germany | he/him | Operations Manager / Élodie | Methodical, reliable, improvement-minded; precise and low-drama; may optimize before challenging a process. |
| `finance_manager` | Sophie Laurent | 39 | France | she/her | Finance Manager / Élodie | Precise, prudent, transparent; numbers-first; can favor safety over speed. |
| `legal_compliance_manager` | James Whitmore | 45 | United Kingdom | he/him | Legal & Compliance Manager / Élodie | Principled, measured, independent; plain-language risk reviewer; can slow bold moves without time-boxing. |
| `people_hr_manager` | Hannah Becker | 36 | Germany | she/her | People & HR Manager / Élodie | Empathetic, candid, fair-minded; supportive and specific; may seek consensus when a decision is required. |
| `chief_product_technology_officer` | Lukas Schneider | 43 | Germany | he/him | Chief Product & Technology Officer / leadership council | Systems-minded, pragmatic, product-led; clear tradeoffs; may underweight emotion and brand perception. |
| `product_manager` | Camille Moreau | 34 | France | she/her | Product Manager / Lukas | Energetic, curious, user-obsessed; evidence-backed storyteller; may prioritize urgency before scalability. |
| `software_architect` | Thomas Reed | 40 | United Kingdom | he/him | Software Architect / Lukas | Inventive, quiet, exacting; diagram- and interface-oriented; can over-design for future complexity. |
| `cybersecurity_manager` | Aisha Khan | 37 | United Kingdom | she/her | Cybersecurity Manager / Lukas | Vigilant, calm, adversarial; firm on critical risk; may overweight worst-case scenarios. |
| `chief_marketing_officer` | Antoine Dubois | 42 | France | he/him | Chief Marketing Officer / leadership council | Charismatic, bold, commercially aware; narrative-driven; can move before evidence is mature. |
| `marketing_strategy_manager` | Charlotte Evans | 35 | United Kingdom | she/her | Marketing Strategy Manager / Antoine | Imaginative, analytical, culturally curious; sharp brief writer; may over-polish strategy. |
| `social_media_manager` | Julien Mercier | 30 | France | he/him | Social Media Manager / Antoine | Witty, quick, observant; channel-native; can overvalue short-lived trends. |
| `sales_partnerships_manager` | Maximilian Bauer | 37 | Germany | he/him | Sales & Partnerships Manager / Antoine | Persuasive, patient, resilient; relational and attentive; can be optimistic about deal probability. |
| `customer_success_manager` | Lena Fischer | 33 | Germany | she/her | Customer Success Manager / Antoine | Attentive, steady, proactive; reassuring and specific; may over-serve edge cases. |

The roster contains eight women and eight men: six characters from Germany, five
from the United Kingdom, and five from France. Personality is independent of
nationality.

## 4. Administrator customization

Every installation starts with the roster above. A human project administrator
can create a draft character version, preview it, publish it, roll back to an
earlier version, or reset it to the OneBrain default.

### 4.1 Editable fields

- display name, fictional age, country, pronouns, biography, and avatar;
- personality traits, tone, vocabulary, communication style, strengths, and
  watch-outs;
- working preferences, collaboration behavior, role focus, and examples;
- the customer-specific character prompt;
- enabled/paused state and default mode;
- model policy selected from deployment-approved providers and models.

### 4.2 Immutable and server-governed fields

An administrator character prompt cannot override:

- tenant, account, space, purpose, membership, classification, RLS, or approved
  status checks;
- module installation state;
- maximum squad size;
- connector OAuth scopes and capability grants;
- action risk classification, approval requirements, payload hashing, expiry,
  idempotency, or audit;
- prohibited autonomous actions;
- EU processing and provider-routing restrictions.

Reporting lines and pod membership are fixed in the first release. They remain
versioned server-side data so a later organization editor can change them with
cycle, team-size, and authority validation.

Only a signed-in human project administrator may configure, publish, reset, or
roll back agents. Service keys and AI employees cannot administer agent
configuration.

Drafts are length-limited, validated, secret-scanned, and excluded from runtime
selection until published. Active missions keep their pinned agent version;
published changes apply to new missions and conversations.

## 5. Persistent agent runtime

The model is stateless; the OneBrain agent is persistent. Users never paste or
rerun character prompts manually.

### 5.1 Effective prompt stack

For each agent turn, OneBrain compiles this ordered stack:

1. immutable OneBrain safety and tenant policy;
2. immutable role, data, and action contract;
3. the administrator-published character version;
4. the current mission, assignment, and required output schema;
5. scoped conversation summary and approved memory;
6. permission-filtered evidence;
7. allowed tool schemas, budgets, and approval behavior.

Retrieved documents, prior messages, tool results, and other agent messages are
untrusted data. They use the existing nonce-fence/spotlighting approach and
cannot rewrite system or character instructions.

### 5.2 Single-agent conversation

A user selects an employee or mentions one by ID. OneBrain pins the published
agent and model-policy versions, retrieves only accessible evidence, compiles the
turn, calls the selected model, validates any structured tool proposal, streams
the answer, and persists the message, sources, usage, and audit result.

### 5.3 Group mission conversation

Group work is a sequence of separate agent turns, not one model pretending to
be every employee.

1. Clara scopes the goal and names one accountable executive.
2. Clara selects a squad with at most six AI participants.
3. OneBrain builds a shared evidence pack accessible to every participant.
4. Each participant independently submits a cited position.
5. The squad receives one bounded challenge round.
6. The accountable executive produces the domain plan.
7. Clara synthesizes decisions, dependencies, dissent, risks, and actions.
8. The human sponsor reviews and approves consequential actions.

No unrestricted agent-to-agent loop is permitted. A mission pins turn, token,
time, and cost budgets. If information visible to one employee cannot be safely
shared with the entire squad, it remains private and may contribute only through
a disclosure-checked summary.

### 5.4 Memory

Persistent memory contains approved preferences, facts, decisions, and lessons
with source provenance, scope, classification, retention, and author. Raw chat
does not automatically become trusted memory. Character changes require an
administrator-published version; feedback never silently changes personality.

Agents wake for a user message, schedule, approved event, or background job.
There are no 16 permanently running model processes.

## 6. Multi-model support

Agent identity, memory, permissions, and conversations are provider-independent.
Provider sessions are optional resumable references, never the only copy of
state.

### 6.1 Runtime interfaces

The module defines:

- a backend registry;
- a normalized streaming event contract for text, structured output, tool
  requests, usage, provider session references, warnings, and errors;
- per-agent default model policy;
- per-task overrides such as `general_reasoning`, `fast_classification`, and
  `code_agent`;
- administrator-approved fallbacks;
- data-classification, processing-region, availability, and cost routing;
- fail-closed handling when no allowed backend is available.

### 6.2 Initial activation

All 16 default employees use Gemini during the initial live test. Gemini uses
the existing LLM configuration and EU-sovereign routing guard. Gemini interaction
IDs and prompt caching may optimize a run, but OneBrain stores the canonical
conversation.

The first implementation includes multi-provider interfaces, policies, provider
health, and contract tests. Claude chat and Claude technical-agent backends are
configuration-gated and inactive until Anthropic credentials, processing policy,
and an isolated coding environment are approved. Enabling Claude later does not
require changing employee IDs or stored conversations.

### 6.3 Technical-agent boundary

Future Claude Agent SDK execution for Lukas, Thomas, and Aisha runs in an
isolated temporary repository worktree/container. The workspace excludes `.env`,
credentials, user home configuration, production data, and unrestricted host
paths. Read, search, testing, and patch preparation can be allowed. PR creation,
merge, deployment, infrastructure, access, secret, and destructive tools remain
approval-gated.

## 7. Persistence model

Dedicated module tables store operational agent state. Customer work products
remain OneBrain intake records so retrieval, privacy, retention, and audit rules
stay unified.

### 7.1 Module records

- `ai_employee_profiles`: stable employee ID, account, role, reporting line, pod,
  status, and default version reference;
- `ai_employee_versions`: immutable published/draft character and prompt
  versions, checksum, author, timestamps, and publication state;
- `ai_employee_model_policies`: default backend/model, task overrides, allowed
  fallbacks, data ceiling, budget, and version;
- `ai_employee_capability_grants`: employee, space, capability, access mode, and
  administrator grant;
- `ai_employee_conversations`: account/space scope, selected employee or mission,
  human owner, and status;
- `ai_employee_messages`: speaker type/ID, visibility, content, citations, pinned
  version, run reference, and timestamp;
- `ai_missions`: goal, sponsor, accountable executive, scope, status, budgets,
  deadlines, and synthesis references;
- `ai_mission_participants`: mission, employee, role in mission, pinned agent and
  model-policy versions, and join/leave status;
- `ai_agent_runs`: backend, model, provider session reference, sources, tools,
  tokens, cost, timing, status, warning, and sanitized error;
- `ai_employee_memories`: approved memory, provenance, classification, scope,
  retention, author, and status;
- `ai_connector_bindings`: provider credential metadata reference, calendar or
  resource allowlist, employee/capability assignment, and status.

Every account/space record is protected by RLS and application-level scope
checks. Memory and Postgres store implementations must expose matching behavior
for tests and local development.

### 7.2 Work products and actions

Reports, briefs, tasks, checklists, plans, policies, and action proposals are
stored in `intake_records` with `app_id=ai_employees`. The intake vocabulary is
extended with any missing approved record types and intents.

Action proposals contain employee, mission, action type, target system, risk,
classification, actionability, source record IDs, payload summary, canonical
payload hash, approver role, expiry, idempotency key, status, and reason.

Approvals and execution outcomes produce immutable action/audit records and
platform audit events without raw secrets or unrestricted content.

## 8. Purpose and authorization model

The module retains the existing read/configure/propose/approve purposes and adds
explicit runtime purposes:

- `ai_employee_read`;
- `ai_employee_configure`;
- `ai_employee_mission_run`;
- `ai_employee_action_propose`;
- `ai_employee_action_approve`;
- `ai_employee_connector_manage`;
- `ai_employee_action_execute`.

Configuration and connector management require a human project administrator.
Approval requires a fresh human session and cannot be performed by a service
key or AI employee. Execution uses a narrowly scoped service principal and must
match the approved action, payload hash, connector binding, capability grant,
expiry, and idempotency key.

An employee profile never grants access by itself. Data access is derived from
the installed app, account, space, human/member scope, service principal,
purpose, category, classification, and approved record status.

## 9. Productive action model

### 9.1 Tiers

1. **Read and analyze:** approved, purpose-scoped data with no external mutation.
2. **Internal creation:** audited OneBrain reports, briefs, tasks, checklists,
   drafts, alerts, missions, and proposals.
3. **Approval-gated connector write:** external or consequential mutations.
4. **Prohibited autonomy:** payments, legal signatures, employment decisions,
   privilege changes, production changes, destructive privacy actions, or policy
   bypass.

### 9.2 Default role capabilities

| Role | Automatic internal work | Important connected systems | Approval-gated examples |
| --- | --- | --- | --- |
| Chief of Staff | Missions, agendas, decisions, tasks, weekly briefs | Calendar, tasks, team chat, KPIs | External invitations, rescheduling others, broadcasts |
| Corporate Strategy | Market scans, scenarios, strategy reports | Approved research, BI, documents, KPIs | Publishing strategy, resource commitments, external claims |
| COO / Operations | Operating plans, SOPs, checklists, risk and bottleneck reports | Calendar, project management, helpdesk, BI | Vendor, staffing, SLA, or customer-impacting changes |
| Finance | Cash, variance, forecast, invoice-aging, and finance reports | Accounting/billing/bank read-only, spreadsheets | Payments, refunds, payroll, invoices, exports, discounts |
| Legal & Compliance | Contract reviews, clause summaries, policies, deadlines | Contract repository, e-sign read-only, calendar, privacy audit | Signing, accepting terms, filings, deletion, export |
| People & HR | Onboarding, interview kits, policies, training, anonymized reports | HRIS limited read, calendar, tasks | Hiring, dismissal, compensation, ratings, employee messages |
| CPTO / Product | Roadmap, PRDs, feedback clusters, release readiness | Repositories, issue tracker, product analytics, observability | Roadmap promises, code/infra/access/production changes |
| Software Architect | ADRs, dependency maps, technical issues, tests, sandbox patches | Repository/CI read, issue tracker, observability | PR creation, merge, deploy, infrastructure, secrets |
| Cybersecurity | Threat models, reports, incident tickets, controls, policy blocks | Audit, scanner, SIEM read, repository security | Account disablement, rotation, permission or containment writes |
| CMO / Marketing | Growth plans, campaigns, content themes, KPI reports | Analytics, CRM/ad/social read | Spend, campaign activation, publishing, public claims |
| Social Media | Calendars, post drafts, reply suggestions, trend reports | LinkedIn, Meta/Instagram, X, social scheduler | Scheduling, publishing, public replies, moderation |
| Sales & Partnerships | CRM notes, follow-ups, proposals, pipeline reports | CRM, email, calendar, documents | Sending, pricing, discounts, terms, opportunity commitments |
| Customer Success | Onboarding, QBRs, account health, escalations, renewals | Helpdesk, CRM, email, calendar | External replies, discounts, credits, refunds, commitments |

## 10. Google Workspace Calendar connector

Google Workspace Calendar is the first real provider adapter.

### 10.1 Connection

A human project administrator completes OAuth, selects allowed calendars, and
grants only the required read and events-write scopes. OneBrain stores provider
and account metadata plus an opaque secret reference; raw access and refresh
tokens never enter agent prompts, action records, logs, or audit metadata.

The administrator assigns named calendar capabilities to individual employees
or roles. Removing the connector or revoking a grant blocks future actions
immediately.

### 10.2 Calendar behavior

When explicitly enabled, an employee may automatically create a private,
self-only focus block or reminder in an allowlisted calendar. The event must not
contain restricted source content.

Human approval is required to:

- add or notify attendees;
- invite external addresses;
- move or cancel another person's event;
- edit an event not created through the same approved binding;
- include confidential or restricted context;
- create a commitment on behalf of the company or another person.

Every write records connector binding, employee, mission, calendar, normalized
event payload hash, idempotency key, policy decision, approval when required,
provider event ID, and sanitized provider response. Retries query by idempotency
metadata or stored execution state and never create a duplicate blindly.

## 11. API surface

The module exposes account/space-scoped human endpoints under
`/api/ai-employees` and narrowly scoped service endpoints under
`/api/service/ai-employees`.

Human endpoints cover:

- module workspaces and posture;
- roster, agent details, drafts, preview, publish, reset, and rollback;
- model policies, provider health, and allowed model catalog;
- conversations, participants, messages, and streaming turns;
- mission creation, squad proposal, start, cancel, detail, and results;
- approved memory review and deletion;
- action proposals, approval/rejection/change requests, and execution status;
- connectors, OAuth status, calendar allowlists, capability grants, and revoke;
- aggregate productivity, risk, cost, failure, and approval metrics.

Service endpoints accept normalized agent records, tool results, connector
callbacks, and worker state. Service keys cannot configure agents or approve
actions.

## 12. Admin and user experience

The AI Employees Module has these views:

1. **Team:** hierarchy, characters, modes, provider, health, current workload.
2. **Agent editor:** character fields, prompt, examples, preview, diff, version
   history, publish, rollback, and reset.
3. **Missions:** goal, sponsor, squad, sources, budgets, progress, transcript,
   dissent, synthesis, and actions.
4. **Chats:** direct employee conversations and mission group chat with distinct
   speakers and citations.
5. **Actions:** proposals, risk, payload preview/hash, approver, expiry, decision,
   execution, and audit.
6. **Connections:** Google Workspace status, calendars, OAuth scopes, grants,
   provider health, and revoke.
7. **Models:** default and task-specific model policies, configured providers,
   cost limits, data ceilings, and health. All defaults initially show Gemini.
8. **Reports:** work created, accepted suggestions, time to approval, blocked
   actions, failures, token/cost usage, and stale queues.

The module UI is hidden when the app is not installed. A paused installation
shows a read-only posture/history view and blocks new work.

## 13. Failure handling

- Retry only transient, non-mutating model/retrieval operations automatically.
- Connector writes use idempotency and are never replayed blindly.
- If no configured backend is permitted for the data classification, fail
  closed and show the administrator the routing reason.
- If Clara or the accountable executive fails, pause the mission.
- An optional specialist failure produces an incomplete result; consequential
  action proposals remain blocked until a human reviews the missing input.
- Invalid, secret-bearing, or oversized character drafts cannot be published.
- Provider errors are sanitized; raw tokens, prompts containing restricted
  content, and provider response bodies do not enter audit metadata.
- Mission concurrency uses durable claims/locks so two workers cannot advance
  the same turn or execute the same action.
- Cancellation stops new model/tool work, preserves the audit trail, and never
  claims an in-flight external write was undone.
- Revoked connectors, paused employees, disabled modules, expired approvals,
  changed payloads, and changed capability grants fail before execution.

## 14. Testing and acceptance

### 14.1 Contract and unit tests

- exactly 16 default employees with stable IDs and the approved hierarchy;
- every standing team and mission squad respects the size limits;
- editable character fields cannot override immutable policy;
- draft, publish, reset, rollback, and active-mission version pinning;
- administrator-only configuration and human-only approval;
- provider-neutral backend contract, model routing, cost/data ceilings, and
  fail-closed behavior;
- all default model policies resolve to Gemini;
- future Claude configuration cannot become active without credentials and
  processing approval;
- prompt stack ordering, nonce fencing, source citations, and output schemas;
- shared evidence is visible to every squad member;
- memory requires provenance and approval;
- proposal hashing, approval, expiry, idempotency, and audit behavior;
- module installation, pause, reactivation, and privacy deletion behavior.

### 14.2 API and integration tests

- direct Gemini-backed employee conversation;
- mission creation, squad selection, independent round, challenge round, domain
  plan, Clara synthesis, and human review;
- worker restart and retry without duplicated turns or actions;
- internal finance report and other role work products stored as scoped intake
  records;
- Google OAuth state/nonce validation, credential references, calendar allowlist,
  self-only event creation, approval-gated invitations/changes, idempotent retry,
  revoke, and provider failure;
- account, space, app, purpose, category, classification, and RLS isolation;
- no raw secrets or inaccessible source content in messages, actions, metrics,
  logs, or audits.

### 14.3 Frontend and end-to-end tests

- hierarchy and 16 character profiles render responsively;
- project admin can edit, preview, publish, and roll back an employee;
- users can select an employee and stream a scoped response;
- mission group chat displays distinct speakers, citations, dissent, and status;
- approval UI shows the exact normalized payload and hash;
- connector and model views show disabled, healthy, degraded, revoked, and
  unconfigured states;
- lint, typecheck, production build, Python test suite, migration validation,
  and secret scan pass.

## 15. Delivery and live activation

1. Add schema migrations, stores, contracts, and module installation guards.
2. Seed the 16 default agents idempotently when AI Employees is installed.
3. Add character administration and all-Gemini model policy.
4. Add direct conversations, approved memory, missions, and bounded team chat.
5. Add internal work products, proposals, approvals, execution policy, and
   metrics.
6. Add Google Workspace Calendar connection and controlled event creation.
7. Add the complete module UI and generated API client/types.
8. Run the full local and CI verification suite.
9. Publish immutable API, worker, and admin UI images from `main`.
10. Register and deploy the candidate to the dummy-data development gate.
11. Configure Gemini and Google OAuth credentials through deployment secrets,
    install AI Employees, and run end-to-end smoke tests.
12. Promote or roll out to a customer only through the existing explicit,
    signed, recoverable release process.

No production activation may silently substitute Railway for the documented
Hetzner path, bypass the development gate, expose Mission Control routes, or put
provider credentials in application records.

## 16. Initial out of scope

- autonomous payments, refunds, payroll, legal signatures, employment
  decisions, access changes, production deployment, destructive security
  containment, or privacy deletion;
- autonomous external email, social publishing, public replies, or contractual
  commitments;
- live CRM, accounting, HRIS, social, repository, CI, SIEM, or helpdesk adapters
  beyond their capability contracts;
- active Claude execution in the initial Gemini-only test;
- administrator editing of reporting lines or arbitrary creation of additional
  employee roles;
- unrestricted background autonomy or all-16 group conversations.

## 17. Done criteria

- AI Employees can be installed, omitted, paused, reactivated, and erased as one
  optional module.
- Every installation receives the approved 16-person European roster.
- Project administrators can safely customize and version every character.
- Users can talk to one persistent Gemini-backed employee without manually
  supplying a prompt.
- Clara can run a bounded, cited, maximum-six team mission with distinct agent
  turns and human review.
- Employees create useful internal reports, tasks, briefs, and proposals from
  approved in-scope data.
- Model routing is provider-neutral while all initial employee policies use
  Gemini.
- Google Calendar can create approved self-only events and cannot mutate other
  people's schedules without human approval.
- Every action is scoped, payload-bound, idempotent, auditable, and secret-safe.
- The complete test suite and development-gate smoke tests pass before a live
  rollout is considered successful.
