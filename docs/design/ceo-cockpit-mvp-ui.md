# CEO Cockpit MVP UI

Date: 2026-06-09

Scope: 3-day MVP UI only. This design is based on `docs/research/ai-company-os-research-plan.md` and the current Company Kernel APIs. It does not include Marketplace, WorkGraph canvas, Skill pricing, distributed renting, complex tenancy, or payment.

## Goal

The CEO Cockpit must answer one question in one screen:

> What is every AI employee doing, what tool did it use, what did it cost, and what evidence did it submit?

This is an operations cockpit, not a chat playground. The UI must be backed by real Company Kernel APIs and database records. No mock data, no "online means active", no ACK-as-done, and no evidence-free completion.

## API Sources

| Area | API |
|---|---|
| Cockpit summary | `GET /v1/dashboard/cockpit` |
| Employee readiness | `GET /v1/agent-matrix` |
| Runtime sessions | `GET /v1/runtime-sessions` |
| Tool calls | `GET /v1/tool-calls` |
| Budget summary | `GET /v1/budget-summary` |
| Budget events | `GET /v1/budget-events` |
| Evidence | `GET /v1/evidence`, `GET /v1/evidence/{id}/content` |
| Task attempts | `GET /v1/tasks/{task_id}/attempts` |
| Trace timeline | `GET /v1/traces/{trace_id}/timeline` |
| Corrections | `POST /v1/tasks/{task_id}/correct` |
| Cancel | `POST /v1/tasks/{task_id}/cancel` |
| Retry / Reassign | `POST /v1/tasks/{task_id}/retry`, `POST /v1/tasks/{task_id}/reassign` |
| Approvals | `GET /v1/approvals`, `POST /v1/approvals/{approval_id}/approve`, `POST /v1/approvals/{approval_id}/deny` |

## 1. CEO Cockpit Home

Purpose: one-screen owner view of the AI company.

Displayed fields:

- Employees: total, active, candidate, active_ready, online_only, task_unsupported, unsafe.
- Tasks: running, stagnant, blocked, done, awaiting approval.
- Runtime: active runtime sessions.
- Tool calls: running, failed/blocked, recent total.
- Budget: total cost, token input/output, runtime seconds.
- Evidence: recent final evidence and evidence issues.
- Supervisor: latest Hermes correction and stagnant detection activity.

Empty state:

- "No active AI employees or tasks yet. Register an employee or submit a task."
- Show primary actions: "Create employee", "Submit task", "Run local smoke".

Abnormal state:

- API offline: show a red read-only banner, keep last rendered data dimmed.
- Budget hard limit exceeded: red global banner, disable new task run actions.
- No real ledger records: show "No runtime/tool/budget records yet", not fake cards.

Click actions:

- Click `stagnant_tasks` filters running task cards.
- Click `tool_calls` opens Tool Calls panel.
- Click `estimated_cost` opens Budget panel.
- Click `recent_evidence` opens Evidence panel.

API:

- Primary: `GET /v1/dashboard/cockpit`
- Secondary: `GET /v1/runtime-sessions`, `GET /v1/tool-calls`, `GET /v1/budget-summary`

## 2. Employee Cards

Purpose: show whether an AI employee can really work, not just whether it is online.

Displayed fields:

- `id`, `name`, `runtime`, `role`.
- readiness badge: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
- heartbeat status and last seen time.
- current task and current attempt, if any.
- active runtime session id.
- latest tool call.
- today/current total cost.
- evidence count.

Empty state:

- No employees: "No AI employees registered."
- Employee idle: "Idle, awaiting task assignment."
- Skill worker with no chat: "No chat; task/evidence only."

Abnormal state:

- `candidate_only`: grey badge, "Needs structured runtime evidence before activation."
- `online_only`: yellow badge, "Online but no task/evidence proof."
- `task_unsupported`: neutral badge, hide chat/direct button, keep task/evidence monitor.
- `unsafe`: red badge, disable automatic assignment.
- stale heartbeat: orange border and "Heartbeat stale".

Click actions:

- Open Employee Detail drawer.
- `View tasks`: filter tasks by employee.
- `View tool calls`: filter `/v1/tool-calls?employee_id=...`.
- `View sessions`: filter `/v1/runtime-sessions?employee_id=...`.
- Hide "Send message" when runtime is `skill` or readiness is `task_unsupported`.

API:

- `GET /v1/dashboard/cockpit`
- `GET /v1/agent-matrix`
- `GET /v1/runtime-sessions?employee_id={id}`
- `GET /v1/tool-calls?employee_id={id}`

## 3. Running Task Cards

Purpose: show active work and intervention options.

Displayed fields:

- `task_id`, title, priority, status.
- assigned/claimed employee.
- `trace_id`, latest `attempt_id`.
- long-task state: running, stagnant, correcting, blocked, failed, cancelled, done.
- heartbeat freshness and progress freshness.
- latest progress message.
- latest tool call.
- current estimated cost.
- final evidence status.

Empty state:

- "No running tasks."
- If submitted but unclaimed: "Awaiting employee claim."

Abnormal state:

- `stagnant`: orange top bar, message: "Employee still online, but no new progress for N minutes."
- `blocked`: red bar with blocker reason.
- `failed`: red bar with failed attempt/error.
- `cancelled`: grey disabled state; old attempt cannot submit done.
- evidence missing on done-like task: "Done-like status without final evidence."

Click actions:

- Open Task Detail drawer.
- `Send Correction`: `POST /v1/tasks/{task_id}/correct`.
- `Cancel`: `POST /v1/tasks/{task_id}/cancel`.
- `Retry`: `POST /v1/tasks/{task_id}/retry`.
- `Reassign`: `POST /v1/tasks/{task_id}/reassign`.
- `View trace`: `GET /v1/traces/{trace_id}/timeline`.

API:

- `GET /v1/dashboard/cockpit`
- `GET /v1/tasks/{task_id}/attempts`
- `GET /v1/traces/{trace_id}/timeline`

## 4. Tool Calls Panel

Purpose: show what employees actually did at the tool/action layer.

Displayed fields:

- `tool_call_id`.
- employee id.
- task id and attempt id.
- session id.
- tool name and type: shell, browser, file, api, openclaw, telegram, local_script, model, other.
- status: running, success, failed, blocked, cancelled.
- risk level.
- input summary.
- output summary.
- started/finished time.

Empty state:

- "No tool calls yet. Tool calls appear when employees run commands, use APIs, read/write files, or operate adapters."

Abnormal state:

- `failed`: red row, show sanitized error summary.
- `blocked`: yellow row, show policy/approval reason.
- `running` too long: orange "long running tool call".

Click actions:

- Open tool call detail modal.
- Filter by task, attempt, employee, session.
- Jump to task drawer.
- Jump to trace timeline.

API:

- `GET /v1/tool-calls`
- `GET /v1/tool-calls?task_id={task_id}`
- `GET /v1/tool-calls?attempt_id={attempt_id}`

Security:

- Never show raw stdout/stderr by default.
- Use sanitized summaries only.
- Hide tokens, `.env`, `~/.ssh`, `api key`, absolute private paths.

## 5. Budget Panel

Purpose: show what the AI company spent.

Displayed fields:

- total amount and currency.
- token input/output.
- runtime seconds.
- cost by employee.
- cost by task.
- cost by cost type: model_api, local_compute, external_api, human_review, other.
- recent budget events.

Empty state:

- "No budget events yet. Costs appear when adapters record model/tool/runtime usage."

Abnormal state:

- over soft limit: orange warning.
- over hard limit: red "budget locked" state.
- missing currency or mixed currency: show "mixed currency" warning.

Click actions:

- Click employee cost: filter employee.
- Click task cost: open task drawer.
- Click event: open trace timeline.

API:

- `GET /v1/budget-summary`
- `GET /v1/budget-summary?task_id={task_id}`
- `GET /v1/budget-summary?employee_id={employee_id}`
- `GET /v1/budget-events`

MVP rule:

- Budget is an estimated ledger, not real payment.
- No Stripe, wallet, rental billing, marketplace settlement, or multi-tenant invoices.

## 6. Evidence Panel

Purpose: show proof of delivery.

Displayed fields:

- evidence id.
- task id, attempt id, trace id.
- employee id.
- artifact id.
- type: file, link, text, screenshot, log.
- summary.
- final/non-final.
- safe relative display path.
- checksum.
- created time.

Empty state:

- "No evidence submitted yet."

Abnormal state:

- unsafe path: red row, no preview link.
- final task without evidence: red "completion invalid" marker.
- superseded/rejected artifact: grey and not included downstream by default.

Click actions:

- Safe preview: `GET /v1/evidence/{evidence_id}/content`.
- Open task drawer.
- Open trace timeline.

API:

- `GET /v1/evidence`
- `GET /v1/evidence?task_id={task_id}`
- `GET /v1/evidence/{evidence_id}/content`

Security:

- Only show workspace/artifacts/evidence/final/reports.
- Do not expose absolute paths.
- Block `../`, `.env`, token, api key, config/profile secrets, `~/.ssh`.

## 7. Task Detail Drawer

Purpose: one task's complete operational truth.

Displayed sections:

- Header: task title, priority, status, target employee, trace id.
- Attempts: attempt id, employee, adapter, status, started/finished, heartbeat/progress times.
- Runtime sessions: session id, runtime type, status, heartbeat.
- Tool calls: tool name/type/status/risk and sanitized summaries.
- Budget: total cost, tokens, runtime seconds, event list.
- Timeline: task/event/attempt/session/tool/budget/artifact/handoff/evidence in time order.
- Evidence: final evidence and safe preview link.
- Approvals: pending or decided approvals.

Empty state:

- no attempts: "Task submitted, waiting for claim/run."
- no tool calls: "No tools used yet."
- no evidence: "No evidence submitted yet."

Abnormal state:

- stale attempt: highlight stale point in timeline.
- failed tool call: expand error summary.
- budget exceeded: sticky warning.
- pending approval: sticky yellow action bar.
- cancelled attempt: grey state; disable "accept done".

Click actions:

- `Send Correction`.
- `Cancel Attempt`.
- `Retry`.
- `Reassign`.
- `Approve/Deny`.
- `Open Evidence Preview`.
- `Copy Trace ID`.

API:

- `GET /v1/tasks/{task_id}`
- `GET /v1/tasks/{task_id}/attempts`
- `GET /v1/traces/{trace_id}/timeline`
- `GET /v1/tool-calls?task_id={task_id}`
- `GET /v1/runtime-sessions?task_id={task_id}`
- `GET /v1/budget-summary?task_id={task_id}`
- `GET /v1/evidence?task_id={task_id}`

## 3-Day UI Build Order

### Day 1

- Add CEO Cockpit summary cards for employees, running/stagnant/blocked tasks, runtime sessions, tool calls, budget, evidence.
- Add visible Runtime Sessions and Tool Calls panels using existing API.
- Keep layout simple: cards + tables + right drawer.

### Day 2

- Add Budget panel and budget rows in task drawer.
- Add task drawer timeline sections for attempt/session/tool/budget/evidence.
- Add empty and abnormal states.

### Day 3

- Run real local demo.
- Verify skill worker no-chat action.
- Verify candidate employee stays candidate.
- Verify dashboard shows runtime session, tool call, budget, evidence from real DB.
- Fix only correctness and visibility issues, not visual polish.

## Anti-Scope Rules

Do not build:

- Marketplace.
- WorkGraph canvas.
- Skill pricing page.
- Distributed renting.
- Multi-tenant billing.
- Payment integration.
- Freeform global chat.
- Mock dashboard data.

## Agy Review Summary

Agy reviewed this as a product/frontend reviewer and emphasized:

- CEO Cockpit is an ERP/control surface, not a chat playground.
- Do not spend MVP time on a WorkGraph canvas.
- Main screen should show business-cleaned operations, not raw Langfuse-style spans.
- Marketplace and skill shop must wait until evidence and budget are trustworthy.
- Budget Center MVP is an estimated SQLite ledger, not real billing.

These points are reflected in the MVP design above.
