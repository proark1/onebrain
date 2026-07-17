# AI Employees module

AI Employees is an optional, tenant- and workspace-scoped OneBrain module. It
ships a 16-person European startup team with persistent identities, editable
character versions, direct chats, bounded cross-functional missions, durable
work products, and approval-gated actions.

## Organization

- Chief of Staff office: AI Chief of Staff and Corporate Strategy Manager.
- Operations & corporate pod: COO, Operations, Finance, Legal & Compliance,
  and People & HR.
- Product, technology & security pod: Chief Product & Technology Officer,
  Product Manager, Software Architect, and Cybersecurity Manager.
- Market & customer pod: CMO, Marketing Strategy, Social Media, Sales &
  Partnerships, and Customer Success.

No standing pod contains more than five employees. A mission squad contains at
most six participants. The Chief of Staff scopes the assignment and synthesizes
the final answer; specialists take positions, challenge each other, and produce
one accountable plan.

## Persistent agents and models

An employee is a durable OneBrain profile, not a prompt copied into every chat.
The store keeps the active character version, model policy, conversations,
messages, approved memory, missions, agent runs, work products, connector
bindings, and action proposals. Each model call assembles the governed system
contract, published character version, current assignment, authorized context,
and relevant approved memory.

Provider routing is model-agnostic. Gemini is the default for the initial
rollout. Anthropic and local backends are registered behind the same policy
boundary and fail closed when they are unavailable or the data classification
exceeds their configured ceiling.

## Run reliability and reconnects

Direct model turns are at-least-once operations with an idempotency key and a
token-fenced, expiring lease. A browser reconnect must reuse its current key so
the runtime returns the existing safe result/state instead of silently starting
a second paid provider call. An intentional retry uses a new key.

The running owner heartbeats its lease and may persist a terminal result only
while its token is current. A disconnect, cancellation, provider timeout, or
process failure cannot let a stale owner overwrite a reclaimed turn. Keep the
provider timeout below the configured run lease so a heartbeat can occur, and
alert on expired/lost leases rather than manually forcing a terminal result.

## Admin controls

The OneBrain project admin can open **AI Employees → Admin** to edit a
character draft and publish a new immutable version. Existing conversations and
runs retain their pinned version for auditability. The admin can pause an
employee without deleting history and can inspect the current provider/model
posture for every role.

The app must be installed in a business or shared space with explicit
`ai_employee_*` purposes. Character metadata never grants access. Every read,
mission, connector, approval, and execution is authorized again from the human
session, account, space, installation state, allowed purpose, and capability
grant.

## Actions and Google Calendar

Employees first create source-bound work products or action proposals. A
proposal records the exact payload hash, source records, risk, expiry,
idempotency key, required approver, and decision history. Consequential actions
require a fresh human session and matching role approval. The executor rejects
changed payloads, expired approvals, missing connector grants, and prohibited
autonomy.

Google Calendar uses OAuth 2.0 authorization code flow with PKCE, signed
one-time state, offline access, narrow scopes, calendar allowlists, employee
capability grants, encrypted token storage, deterministic event ids, and
idempotent conflict recovery. A private, self-only focus block is the only
default limited-automation exception. Email, publishing, payments, HR/legal
commitments, permissions, production changes, and destructive privacy/security
actions remain approval-gated or prohibited.

Configure these server-side values to enable Calendar:

```text
ONEBRAIN_AI_EMPLOYEES_GOOGLE_CLIENT_ID
ONEBRAIN_AI_EMPLOYEES_GOOGLE_CLIENT_SECRET
ONEBRAIN_AI_EMPLOYEES_GOOGLE_REDIRECT_URI
ONEBRAIN_SECRET_ENCRYPTION_KEY
```

OAuth tokens never enter normal records, API responses, model prompts, or
exports. GDPR export includes AI employee records with credential references
redacted. Erasure removes the scoped AI records, encrypted connector secrets,
and pending OAuth state, subject to the existing legal-hold gate.

## Verification

Run the backend and web checks before rollout:

```powershell
python -m ruff check app tests
python -m pytest -q
cd onebrain-web
npm run openapi
npm run typecheck
npm run lint
npm run build
```
