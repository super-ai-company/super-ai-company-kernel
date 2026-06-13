# CEO Dashboard Simplification Refactor Plan

Date: 2026-06-09

Source: three-round Codex <-> Antigravity design review through Company Kernel direct messaging.

## Goal

Make the local dashboard simple enough that the owner can understand the company state in 30 seconds:

- Which AI employees are usable, busy, candidate-only, unsafe, or stale.
- What each running task is doing and whether it is progressing.
- Which tasks need owner attention: stagnant, blocked, pending approval, invalid completion, or over budget.
- What money/tokens/time were spent.
- What final evidence exists and whether it is safe to review.

This is a page-function and information-architecture refactor. It must not weaken the Kernel ledger, task state machine, evidence rules, approval gates, or cost accounting.

## P0: Three-Day MVP

### 1. Health Bar And CEO Summary

Goal: establish trust immediately.

Page area:
- Top strip and first metric row.

Real API data:
- `GET /v1/doctor`
- `GET /v1/dashboard/cockpit`

Fields to show:
- API online/offline.
- Doctor issue count.
- Last successful sync time.
- Owner attention count.
- Active-ready employees.
- Running tasks.
- Stagnant/blocked tasks.
- Pending approvals.
- Valid final evidence.
- Invalid completions.
- Total spend by currency.

Empty state:
- `Waiting for Kernel API`
- `No employees registered`

Exception state:
- API offline: dim dashboard and show last successful sync.
- Doctor unhealthy: show issue count and owner next action.
- Ledger gap: show `Tool/cost ledger missing`; never mock data.

Click actions:
- Refresh now.
- Open doctor detail modal.

Acceptance:
- Numbers match `/v1/dashboard/cockpit` and `doctor --summary`.
- No fake static placeholder numbers.

### 2. Employee Cards

Goal: show usable labor capacity without pretending candidates are active.

Page area:
- Employee grid.

Real API data:
- `GET /v1/dashboard/cockpit`
- Employee `readiness_level`, `work_health`, `work_status_summary`, `budget_summary`, `evidence_summary`.

Always show:
- Name/id.
- Role/runtime.
- Readiness badge.
- Work health: `ok/warn/block`.
- Online/stale state.
- Current task title.
- Today's or total spend.

Default hidden:
- Attempt/session ids.
- Token breakdown.
- Tool-call counts.
- Raw capabilities text.

Click to detail:
- Work history.
- Tool calls.
- Runtime sessions.
- Evidence.
- Budget ledger.

Exception state:
- `candidate_only`: grey, not schedulable.
- `online_only`: warning, online is not active-ready.
- `unsafe`: red, no task assignment.
- Skill worker: no chat button, but task/evidence progress visible.

Acceptance:
- Candidate employees cannot look like ready employees.
- Skill workers do not show chat actions.
- Work health reasons are visible enough to explain why an employee needs attention.

### 3. Actionable Task Cards

Goal: make active work and owner interventions obvious.

Page area:
- First-screen task area, above general history.

Real API data:
- `GET /v1/dashboard/cockpit`
- `task_cards`, `long_tasks`, `owner_attention`.

Fields to show:
- Task title.
- Assigned employee.
- State: running, stagnant, blocked, pending approval, invalid completion, done.
- Runtime age.
- Latest progress message.
- Evidence state.
- Cost summary.
- Owner next action.

Empty state:
- `No running tasks. Awaiting employee claim.`

Exception state:
- Stagnant: heartbeat fresh but progress stale.
- Done without final evidence: `Completion Invalid`.
- Over budget: red badge and approval required.

Click actions:
- Open task detail drawer.
- Correct.
- Cancel.
- Retry.
- Reassign.
- Approve/deny pending owner actions.

Safety:
- Write actions are loading-locked after click.
- Real execution stays approval-gated; dry-run remains default where configured.

Acceptance:
- Invalid done tasks are impossible to miss.
- Stagnant tasks are not called timeout failures.
- Correct/cancel/retry/reassign are visible but respect owner approval policy.

### 4. Task Detail Drawer

Goal: make details understandable without forcing the owner to read developer traces.

Boss-facing tabs:
- Overview: task goal, owner, employee, state, current blocker, owner next action.
- Deliverables: final evidence, safe preview, checksum/status, accept/reject/request rework.
- Progress: timeline of submitted -> claimed -> attempt -> progress -> evidence -> done.
- Actions: corrections, probes, cancellations, retries, reassigns, approvals.
- Cost & Tools: sanitized tool calls, budget events, model/provider/token/time rollups.

Real API data:
- `GET /v1/tasks/{task_id}`
- `GET /v1/traces/{trace_id}/timeline`
- `GET /v1/evidence/{evidence_id}/content` or existing safe-preview route.
- `GET /v1/tool-calls/{tool_call_id}`
- `GET /v1/runtime-sessions/{session_id}`

Empty state:
- `No progress recorded yet`
- `No final evidence yet`
- `No budget ledger events recorded`

Exception state:
- Unsafe evidence path: disable preview and show reason.
- Raw logs not sanitized: show redacted summary only.
- Missing tool/cost ledger: show ledger gap warning.

Acceptance:
- The owner can inspect task state, progress, cost, tool calls, evidence, and approval context from one drawer.
- Raw stdout/stderr and absolute local paths are not exposed by default.

### 5. Budget And Tool Ledger Panels

Goal: keep cost and behavior auditable without turning the first screen into a developer console.

Page area:
- Collapsed by default on cockpit.
- Detailed inside task and employee drawers.

Real API data:
- `GET /v1/budget-summary`
- `GET /v1/budget-events`
- `GET /v1/tool-calls`

Fields:
- Total spend by currency.
- Token input/output.
- Runtime seconds.
- Provider/model.
- Employee/task/project rollups.
- Latest tool call and failed/blocked calls.

Exception state:
- Soft budget exceeded: orange.
- Hard budget exceeded: red and require approval before more spend.
- Mixed currency: do not convert; show separate rows.
- Unsanitized raw logs: redacted.

Acceptance:
- All spend comes from Kernel budget ledger.
- Frontend does not estimate or invent money.

## P1

- SSE updates for critical events, keeping REST polling fallback.
- Visual task timeline with boss-facing labels.
- Saved filters for employees, blocked tasks, and evidence review.
- Better action confirmation flow for high-risk approvals.

## P2

- Custom reports.
- Retention/prune UI.
- Skill registry marketplace preparation only after local control plane is stable.

## Explicit Non-Goals

- Marketplace.
- Payment and subscription.
- Multi-tenant user system.
- Distributed rental nodes.
- Complex WorkGraph canvas.
- Fancy animations that hide state.
- Chat-first interface.
- Treating online, ACK, stdout, or chat replies as completion.

## 30-Second Owner Acceptance

Within 30 seconds of opening the dashboard, the owner must know:

- Is the system healthy and fresh?
- How many employees are active-ready, limited, candidate-only, unsafe, or stale?
- Who is doing what right now?
- Which tasks are stagnant, blocked, awaiting approval, invalid, or done?
- What has been spent in tokens/time/money?
- Which final evidence is ready for review?
- What is the next safe owner action?

## Antigravity Review Notes

Antigravity's planning response is treated as design review input, not execution evidence. The adapter correctly marked it as planning-only / not activation-eligible because no code, tests, browser verification, or task evidence was produced.
