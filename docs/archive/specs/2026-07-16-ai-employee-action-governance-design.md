# AI Employee Action Governance Design

**Date:** 2026-07-16
**Status:** Draft for implementation planning

## Objective

Extend OneBrain from a shared data layer into a secure team of proactive AI employees. The system must let the eight company-critical AI employees prepare work, flag risks, structure uploaded data, and propose actions while ensuring real employees approve every external, privileged, financial, legal, HR, security, or destructive action.

AI Employees is a deployable customer module, not a mandatory Cockpit-only view. Operators can install the `ai_employees` app when a customer wants governed proactive agents, include it in full-stack deployments, or omit it for customers that only need the core brain, KPI dashboard, communication, or assistant modules.

## Non-negotiable safety rules

- AI employees never receive direct write authority to external systems by default.
- Every AI action starts as an `action` or `policy_decision` intake record with an explicit department, risk level, target system, proposed payload, required approver role, and expiry.
- External sends, payments, account changes, HR decisions, access changes, publishing, deletion, and customer commitments require a real employee approval before execution.
- Low-risk internal work is still auditable: creating an internal draft, checklist, reminder, or summary records an `action_audit` event.
- Access to data is derived from existing account, space, purpose, classification, category, and status controls, not from the persona name.
- Pending, confidential, restricted, or low-confidence data cannot become proactive context until it is approved or routed to the correct reviewer.
- Agent prompts are not trusted policy. Authorization, approval, and data filtering remain server-side checks.

## Existing foundation to reuse

OneBrain already has the core pieces needed for a secure proactive model:

- Structured intake records have account, space, app, purpose, record type, intent, classification, confidence, status, summary, extracted facts, and metadata.
- The intake pipeline detects PII, classifies data, extracts facts, and parks confidential, restricted, low-confidence, or approval-required records as pending.
- Retrieval access already enforces tenant, account, space, private-space ownership, classification clearance, location, category, and approved status before the model can see context.
- Assistant contracts already include records for actions, action audits, approvals, policy decisions, security decisions, follow-ups, tasks, briefs, settings, provider health, connected accounts, and secret references.
- Platform audit and data-access ledgers already give a place to record decisions without storing raw sensitive content in audit metadata.

## Deployable module contract

- App ID: `ai_employees`.
- Contract version: `ai_employees.v1`.
- Customer bundle: `onebrain_ai_employees`.
- Default spaces: business and shared only.
- Purposes: `ai_employee_read`, `ai_employee_configure`, `ai_employee_action_propose`, and `ai_employee_action_approve`.
- Default runtime mode: draft-only with approval queue; no autonomous external execution.
- Install boundary: deploying the module only grants registered app purposes and scoped spaces. It does not bypass account, tenant, retrieval, service-key, classification, status, approval, or audit checks.

## Control center experience

The AI Employees module should have a dedicated control center in addition to the Cockpit cards:

- employee install/enabled status;
- department owner and required approver;
- allowed data categories and source spaces;
- current mode: silent, suggest-only, draft-only, approval queue, or limited automation;
- pending proposals, approvals, rejections, expiries, and policy blocks;
- data-quality queue for missing departments, stale facts, duplicate records, low confidence, and restricted-data routing;
- productivity signals by employee, such as drafts prepared, risks flagged, accepted suggestions, and time-to-approval;
- security signals by employee, such as blocked actions, high-risk proposals, approval bypass attempts, and payload-hash mismatches.

The first control-center slice can be configuration/reporting-only: it should show module posture, enabled employees, default modes, approval rules, data-quality signals, security signals, and productivity signals before real execution providers are connected.

## Data structure contract

Every upload or service capture should produce a normalized data envelope before agents can use it:

| Field | Purpose |
| --- | --- |
| `department` | Main owning department, such as finance, people, product, engineering, marketing, operations, customer_success, legal, sales, or general. |
| `secondary_departments` | Optional additional teams that may need the record. |
| `record_type` | What the data is: document, message, policy, task, contact, fact, transcript, action, etc. |
| `intent` | Why it matters: question, knowledge update, complaint, sales lead, action proposal, approval, follow-up, etc. |
| `classification` | Public, internal, confidential, or restricted. |
| `category` | Access compartment used by security filters, aligned with department where possible. |
| `status` | Pending until confidence, approval, and classification rules allow retrieval. |
| `actionability` | `answer_only`, `draft_only`, `approval_required`, or `automation_allowed`. |
| `risk_level` | `low`, `medium`, `high`, or `critical`, based on data sensitivity and action impact. |
| `source_confidence` | Confidence in classification, extraction, and ownership. |
| `retention_hint` | Suggested retention policy reference, never a raw policy override. |
| `provenance` | Source app, source reference, ingest job, timestamp, and classifier version. |

The department and actionability labels are the missing bridge between structured data and safe proactive behavior. They should be stored in metadata and duplicated into retrieval metadata where the access filter needs category-level enforcement.

## AI employee roster and authorities

The initial roster remains capped at eight people because these are the highest-value cross-company functions:

1. Finance Manager
2. HR Manager
3. Product Manager
4. Software Architect
5. Marketing Strategy Manager
6. Social Media Manager
7. Operations Manager
8. Customer Success Manager

Each AI employee has a department, allowed purposes, default data categories, proactive signals, safe draft actions, and approval rules. The roster is presentation and routing metadata only. It must not bypass the principal, service-key, purpose, or retrieval filters.

Each employee should also expose product metadata for the control center: default mode, never-without-approval actions, productivity metrics, and human owner role.

## Action lifecycle

1. **Observe:** agent reads only approved, in-scope records and structured facts.
2. **Detect:** agent identifies a risk, opportunity, missing field, follow-up, or draftable action.
3. **Propose:** agent writes an `action` record with risk, target, payload summary, exact proposed action, and required approver.
4. **Review:** a real employee receives the proposed action in their department queue.
5. **Approve or reject:** approval writes an `approval` intent and `action_audit` event.
6. **Execute:** only an execution service with the matching purpose can perform the approved action, using the approved payload hash and idempotency key.
7. **Audit:** execution records `assistant.action.executed`; rejection, expiry, and policy blocks are audited too.

## Proactivity boundaries

Allowed without human approval:

- Drafting internal notes, emails, social posts, specs, policies, and reports.
- Creating internal reminders or checklists inside OneBrain.
- Flagging data-quality issues, missing department labels, possible duplicates, stale facts, and approval needs.
- Summarizing risks and suggesting next actions.

Default mode:

- Draft-only for every new customer installation.
- Proactive insight cards are allowed once data is approved and in-scope.
- Limited automation can be enabled later only for low-risk internal tasks with explicit customer policy.

Requires human approval:

- Sending email, chat, or social posts externally.
- Publishing content.
- Committing money, discounts, refunds, payroll, or contracts.
- Changing employee status, compensation, permissions, or access.
- Changing production infrastructure or code.
- Deleting, exporting, or transferring data.
- Any action based on confidential or restricted data.

Forbidden until separately designed:

- Fully autonomous external communication.
- Autonomous financial transactions.
- Autonomous HR decisions.
- Autonomous privilege escalation or secret access.
- Bypassing pending-review data because an agent requests it.

## Review queues

The product needs department queues that show:

- proposed action;
- source records and departments;
- data classification and risk level;
- exact payload preview;
- required approver role;
- expiry and idempotency key;
- approve, reject, request changes, and mark duplicate actions.

Approvers must see why the agent thinks the action is useful and what data it used, without exposing records outside their own access scope.

Approval cards must include a payload hash. If a payload changes after approval, the old approval is invalid and the changed payload becomes a new proposal.

## Proposal contract

Every proposal should be normalized before it is persisted or shown:

- `employee_id`
- `department`
- `action_type`
- `target_system`
- `risk_level`
- `classification`
- `actionability`
- `source_record_ids`
- `payload_summary`
- `payload_hash`
- `required_approver_role`
- `expires_at`
- `idempotency_key`
- `status`
- `requires_approval`
- `reason`

The payload hash must be computed from a canonical JSON payload with raw secrets rejected. External or privileged action types always require approval, even when the record classification is only internal.

## Tests and acceptance criteria

- Uploads with unknown department, restricted data, PII, low confidence, or missing actionability stay pending.
- Agents cannot retrieve pending records.
- A persona cannot access a department category unless its service principal has that category and purpose.
- External action execution fails without a matching approved action record.
- Approval payload hash mismatches block execution.
- Replayed action execution is idempotent.
- All proposals, approvals, rejections, executions, expiries, and policy blocks are audited without raw secrets.
- Department queues do not leak inaccessible source records.
