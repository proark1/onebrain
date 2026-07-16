# AI Employee Action Governance Implementation Plan

**Date:** 2026-07-16
**Status:** Draft plan
**Design:** `docs/superpowers/specs/2026-07-16-ai-employee-action-governance-design.md`

## Objective

Turn the visible AI employee council into secure proactive assistants that can structure data, prepare work, and propose actions while keeping real employees in control of sensitive or external execution.

AI Employees should ship as a selectable customer module. Like KPI Dashboard and AI Communication, operators should be able to deploy `ai_employees` for customers that want governed proactive agents and omit it for customers that do not.

## Implementation order

### 1. Register the deployable AI Employees module

**Files**

- Modify `app/platform/base.py`.
- Modify `app/routers/platform.py`.
- Modify `app/provisioning/bundles.py`.
- Modify `app/provisioning/service.py`.
- Add/modify `tests/test_provisioning.py`.

**Test first**

- `ai_employees` is accepted as a platform app.
- `ai_employee_read`, `ai_employee_configure`, `ai_employee_action_propose`, and `ai_employee_action_approve` are accepted as platform purposes.
- `onebrain_ai_employees` provisions OneBrain Core plus AI Employees only.
- Full-stack provisioning includes AI Employees.
- Other bundles omit AI Employees unless selected.

**Implement**

- Add an `AI_EMPLOYEES_APP` template scoped to business and shared spaces.
- Add a dedicated `onebrain_ai_employees` bundle.
- Include the AI Employees app in the full-stack bundle.
- Keep external execution credentials out of the default module until approval-gated execution is implemented.

### 2. Formalize department and actionability labels

**Files**

- Modify `app/intake/base.py`.
- Modify `app/intake/pipeline.py`.
- Modify `tests/test_intake.py`.
- Update `docs/onebrain-intake-pipeline.md`.

**Test first**

- Uploads are assigned a primary department and optional secondary departments.
- Finance, HR, product, engineering, marketing, operations, customer success, legal, sales, and general labels normalize deterministically.
- Unknown or low-confidence department detection parks the record as pending.
- `actionability` is assigned as `answer_only`, `draft_only`, `approval_required`, or `automation_allowed`.
- Confidential or restricted data can never be `automation_allowed`.

**Implement**

- Add pure classifiers for department, actionability, and risk level.
- Store labels in `metadata` and `extracted_facts`.
- Mirror the primary department into retrieval metadata as the category when appropriate.

### 3. Add the AI employee contract

**Files**

- Add `app/assistant/employees.py`.
- Modify `app/assistant/contracts.py`.
- Add `tests/test_ai_employee_contracts.py`.

**Test first**

- Exactly eight active AI employees exist.
- Each employee has a stable ID, name, role, department, categories, purposes, safe actions, approval rule, and prompt-safe description.
- Employee metadata never grants access by itself.
- Unknown employee IDs fail closed.

**Implement**

- Move the roster from UI-only data into a shared contract.
- Keep UI portraits and body-culture copy in the web layer, but source role/security metadata from the contract.
- Add default mode, never-without-approval actions, productivity metric names, and human owner role metadata for the module control center.

### 4. Build action proposal records

**Files**

- Extend `app/assistant/contracts.py`.
- Modify `app/routers/assistant.py`.
- Add `tests/test_assistant_actions.py`.

**Test first**

- Agents can create action proposals only with allowed assistant purposes.
- Proposed actions require employee ID, department, action type, target system, risk level, source record IDs, payload summary, payload hash, required approver role, and expiry.
- Proposals referencing inaccessible records fail.
- Raw secrets and raw OAuth tokens are rejected.

**Implement**

- Represent proposed actions as intake records, not assistant-owned tables.
- Use `action`, `policy_decision`, `approval`, and `action_audit` record types.
- Add validation helpers that compute risk and required approvals server-side.
- Add canonical payload hashing and raw-secret rejection before proposals can enter a queue.
- Force external or privileged action types into human approval even if their source data is internal.

### 5. Add human approval queues

**Files**

- Add `app/routers/approvals.py` or extend an existing admin router.
- Add web approval-queue components.
- Add `tests/test_approval.py` coverage for AI action proposals.

**Test first**

- Department owners see only proposals they can access.
- Approve, reject, request changes, expire, and duplicate decisions are audited.
- Approval requires a fresh human session and cannot be performed by a service key.
- Approvers cannot change payloads silently; changed payloads become new proposals.
- Approval cards show source records, confidence, risk, payload hash, expiry, and why the employee proposed the action.

**Implement**

- List pending proposals by account, space, department, risk, and approver role.
- Store approval decisions as `approval` intent records and platform audit events.

### 6. Gate execution by approval

**Files**

- Add execution-policy helpers under `app/assistant/` or `app/security/`.
- Extend service endpoints only where real execution is needed.
- Add tests for email, social, task, and data-export gates before integrating providers.

**Test first**

- Execution without approval fails.
- Wrong approver role fails.
- Expired approval fails.
- Payload hash mismatch fails.
- Duplicate execution returns the original result.
- Confidential or restricted data forces approval even for normally low-risk actions.

**Implement**

- Require an approved action ID and idempotency key for execution endpoints.
- Bind execution to the approved payload hash and service-key purpose.
- Audit every decision and execution result.

### 7. Add the AI Employee Control Center

**Files**

- Add web module pages/components for AI Employees.
- Extend cockpit observability APIs.
- Extend platform audit/data-access reports.

**Test first**

- Admins can see which customers have AI Employees installed.
- Admins can enable, pause, or draft-lock each employee.
- Admins can assign department approvers.
- Users can see pending proposals, approval state, productivity signals, and security blocks.
- No raw restricted content leaks into metrics cards.

**Implement**

- Add module status, mode controls, approval queues, data-quality queue, productivity metrics, and security metrics.
- Add weekly AI employee standup summaries for work prepared, risks found, approvals waiting, and data-quality blockers.
- Ship the first page as read-only/configuration guidance before provider execution exists.

### 8. Update the Cockpit and employee council UI

**Files**

- Modify `onebrain-web/src/components/cockpit-panel.tsx`.
- Modify `onebrain-web/src/app/globals.css`.

**Test first**

- The council explains proactive behavior and approval boundaries.
- The council explains AI Employees as an optional deployable module.
- Each employee shows department, proactive mode, safe actions, and approval rule.
- Each employee shows default mode, never-without-approval guardrails, and productivity signals.
- Responsive layout remains usable on desktop, tablet, and mobile.

**Implement**

- Keep the roster visible in Cockpit.
- Add status copy that makes clear AI employees prepare and propose; humans approve sensitive execution.
- Add module/control-center copy so operators understand installation, approval queues, data quality, productivity, and security monitoring.

### 9. Add operational reports

**Files**

- Extend cockpit observability APIs.
- Extend platform audit/data-access reports.

**Test first**

- Operators can see counts of pending proposals, approved actions, rejected actions, expired actions, and policy blocks.
- Reports aggregate by department and employee without exposing raw content.

**Implement**

- Add dashboard metrics for action-governance health.
- Add alerts for stale approval queues or repeated policy blocks.

## Done criteria

- Every upload is classified by type, intent, department, sensitivity, status, risk, and actionability.
- Agents can proactively propose useful work from approved in-scope data.
- AI Employees can be installed, omitted, paused, and monitored as a customer module.
- Agents cannot execute sensitive or external actions without human approval.
- Human approval queues are auditable, scoped, and payload-bound.
- Execution is idempotent and tied to a valid approval.
- Operators can monitor proposal and execution health without seeing sensitive content.
