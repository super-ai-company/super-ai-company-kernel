# CEO Cockpit 3-Day MVP UI Contract

Date: 2026-06-09

Scope: local Super AI Company Kernel CEO Cockpit MVP. This is a UI and API implementation contract for the next 3 development days. It intentionally does not design marketplace, WorkGraph canvas, skill pricing, distributed rental, payments, public users, or complex multi-tenant features.

## 1. MVP Goal

The CEO Cockpit must answer one operational question without fake data:

> Which AI employee is doing what, which tools did it use, what did it cost, and what evidence did it submit?

The page is read-heavy and ledger-driven. It must use Company Kernel API/database state as the source of truth. It must not infer completion from chat, ACK, stdout, inbox files, or heartbeat alone.

## 2. Current Backend Mapping

Use existing backend surfaces first. Do not invent new endpoints unless implementation proves the existing endpoint cannot support the UI.

| Need | Existing API | Notes |
|---|---|---|
| Cockpit home aggregate | `GET /v1/dashboard/cockpit` | Primary polling endpoint. Includes `generated_at`; backend owns stale/stagnant/session state. |
| Doctor banner | `GET /v1/doctor` | Browser cannot run `bin/companyctl`; show returned JSON summary in modal. |
| Employees | `GET /v1/employees` | Employee cards and readiness summary. |
| Employee detail | `GET /v1/employees/{employee_id}` | Lazy load from card click. |
| Readiness matrix | `GET /v1/agent-matrix?agents={id}` | Use for evidence-backed readiness badges. |
| Task list | `GET /v1/tasks` | Secondary list if cockpit aggregate lacks enough cards. |
| Task detail drawer | `GET /v1/tasks/{task_id}` | Single call. It already aggregates attempts, runtime sessions, tool calls, budget, evidence, completion contract, and timeline. Avoid N+1 drawer fetches. |
| Trace timeline | `GET /v1/traces/{trace_id}/timeline` | Use only when drawer needs richer trace story than task detail payload. |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=&limit=` | Global Runtime panel; detail drawer prefers task detail payload. |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&trace_id=&attempt_id=&session_id=&limit=200` | Max list cap is 200. Render only sanitized summaries. |
| Budget summary | `GET /v1/budget-summary?employee_id=&task_id=&trace_id=&attempt_id=` | Frontend displays ledger values only; no currency conversion. |
| Budget events | `GET /v1/budget-events?...` | Recent spend rows. |
| Evidence list | `GET /v1/evidence?task_id=&limit=` | Evidence panel. |
| Evidence preview | `GET /v1/evidence/{evidence_id}/content` | Only safe content preview. No direct file path reads. |
| Correction | `POST /v1/tasks/{task_id}/correct` | Body: `attempt_id`, `by="owner"`, `message`. |
| Cancel | `POST /v1/tasks/{task_id}/cancel` | Body: `attempt_id`, `by="owner"`, `reason`. No kill-session button in MVP. |
| Retry | `POST /v1/tasks/{task_id}/retry` | Body: `by="owner"`, `reason`. |
| Reassign | `POST /v1/tasks/{task_id}/reassign` | Body: `by="owner"`, `to`, `reason`. |
| Reject evidence / reopen | `POST /v1/tasks/{task_id}/reopen` | Body: `by="owner"`, `reason`, `status="submitted"`. MVP has reject/reopen, not a new task-accept endpoint. |

## 3. Global UI Rules

1. Poll `/v1/dashboard/cockpit` every 8 seconds. No WebSocket in the 3-day MVP; SSE can remain a future hook.
2. If API fails, keep last successful data visible but dimmed and show `Offline: API Connection Lost`.
3. If `/v1/doctor` returns non-zero `exit_code`, show yellow diagnostics banner with issue count and a modal containing the JSON summary.
4. Do not render raw local absolute paths. Display safe relative paths and basenames only.
5. Do not calculate prices, exchange rates, or token-to-USD conversion in frontend. Display backend ledger totals. If multiple currencies exist, display per-currency rows, not a fake combined total.
6. Do not add `Kill Session`, `Archive Stale Sessions`, marketplace actions, skill pricing, or user/account management in this MVP.
7. Skill-only or `task_unsupported` workers must hide chat/direct buttons, but still show task progress, tool calls, budget, artifacts, and evidence.
8. Candidate employees must not be visually promoted to active. Use explicit badges: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.

## 4. Page Layout

One page, high-density operations console:

1. Top health bar: API state, doctor state, generated time, active sessions, active attempts, pending approvals.
2. CEO summary grid.
3. Employee cards.
4. Running/stagnant/blocked task cards.
5. Three ledger panels: Tool Calls, Budget, Evidence.
6. Right-side Task Detail Drawer.

Do not introduce complex charts. Use CSS grid, tables, small progress bars, and severity badges.

## 5. CEO Cockpit Home

Purpose: show whether the company is healthy and where owner attention is needed.

Displayed fields:
- Generated timestamp from `/v1/dashboard/cockpit.generated_at`.
- Employees: total, online, busy, active_ready, active_limited, candidate_only, online_only, task_unsupported, unsafe.
- Tasks: running, stagnant, blocked, failed, awaiting approval, done, invalid completion.
- Runtime: active sessions, stale sessions, active attempts.
- Tool calls: running, success, failed, blocked.
- Budget: backend total by currency, token input, token output, runtime seconds, soft/hard limit status if present.
- Evidence: recent final evidence count, evidence issues count.
- Supervisor: latest Hermes correction or pending correction ack from `supervisor_activity`.

Empty states:
- No employees: `No AI employees registered.`
- No active tasks: `No running tasks.`
- No budget: `No budget ledger events recorded.`
- No evidence: `No evidence submitted yet.`

Abnormal states:
- API offline: red banner, stale data dimmed.
- Doctor unhealthy: yellow banner, click opens `/v1/doctor` JSON modal.
- Hard budget limit: red banner; disable run/retry/reassign/new paid task actions, but keep cancel/correction/reopen visible.
- Completion invalid: red count and filter to invalid task cards.

Click actions:
- Employee count click: filter Employee Cards by badge/status.
- Stagnant/blocked count click: filter Task Cards.
- Budget click: scroll to Budget Panel.
- Evidence click: scroll to Evidence Panel.
- Doctor banner click: fetch and show `/v1/doctor`.

API:
- Primary: `GET /v1/dashboard/cockpit`
- Diagnostic detail: `GET /v1/doctor`

## 6. Employee Cards

Purpose: make employee readiness honest. Online is not active.

Displayed fields:
- Employee ID, display name, role, runtime/adapter type.
- Readiness badge.
- Status: online, busy, candidate, active-limited, abnormal, unsafe.
- Heartbeat: last seen and freshness label.
- Current work: task id, attempt id, session id, latest progress.
- Capabilities: task/chat/tool support summary.
- Ledger rollup: tool call count, evidence count, cost by currency.

Empty states:
- No employees: `No AI employees registered.`
- Idle employee: `Idle. Awaiting task assignment.`
- Skill worker: `No chat; task/evidence execution only.`

Abnormal states:
- `unsafe`: red border, auto-assignment disabled.
- `candidate_only`: gray card, `Needs structured runtime evidence before activation.`
- `online_only`: yellow badge, `Online but no task/evidence proof.`
- Heartbeat stale: amber badge. Backend-provided stale state wins over browser clock.

Click actions:
- Click card: open Employee Detail drawer or panel using `GET /v1/employees/{employee_id}`.
- `View Task`: open current task drawer.
- `Filter Logs`: apply employee filter to Tool Calls/Budget/Evidence panels.
- `Send Direct Message`: hide/disable if readiness is `task_unsupported` or runtime is skill-only.
- `Verify Runtime Evidence`: call `GET /v1/agent-matrix?agents={employee_id}` and show attendance/direct/runtime/task/progress/evidence/stale results.

API:
- `GET /v1/employees`
- `GET /v1/employees/{employee_id}`
- `GET /v1/agent-matrix?agents={employee_id}`
- Optional filters: `/v1/tool-calls?employee_id=...`, `/v1/budget-summary?employee_id=...`, `/v1/evidence?employee_id=...` if supported; otherwise filter client-side from hydrated rows.

## 7. Running Task Cards

Purpose: show long-running task state without treating CLI timeout as failure.

Displayed fields:
- Task ID, title, priority, status.
- Assigned employee.
- Current attempt ID and trace ID.
- State badge: `running`, `progress_fresh`, `stagnant`, `correcting`, `blocked`, `failed`, `cancelled`, `done`.
- Latest progress message and timestamp.
- Heartbeat/progress freshness.
- Completion contract: valid/invalid and reason.
- Cost summary and evidence count.

Empty states:
- No running tasks: `No running tasks.`
- Submitted but unclaimed: `Awaiting employee claim.`

Abnormal states:
- Stagnant: `Employee is still online, but no new progress for 15 minutes. Continue waiting, send probe, request Hermes correction, or cancel.`
- Blocked: show blocker reason.
- Failed: show failure reason and retry/reassign controls.
- Done without final evidence: red `Completion Invalid`.

Click actions:
- Click card: open Task Detail Drawer via `GET /v1/tasks/{task_id}`.
- `Send Correction`: POST `/v1/tasks/{task_id}/correct` with current `attempt_id`, `by="owner"`, user message.
- `Cancel Attempt`: POST `/v1/tasks/{task_id}/cancel` with current `attempt_id`, `by="owner"`, reason.
- `Retry`: POST `/v1/tasks/{task_id}/retry`.
- `Reassign`: POST `/v1/tasks/{task_id}/reassign`.
- `Reject / Reopen`: for done evidence that fails owner review, POST `/v1/tasks/{task_id}/reopen` with `status="submitted"`.

API:
- Primary card data: `GET /v1/dashboard/cockpit`
- Drawer: `GET /v1/tasks/{task_id}`

## 8. Tool Calls Panel

Purpose: answer what tools employees used.

Displayed fields:
- Tool call ID, created/started/finished time.
- Employee ID, task ID, trace ID, attempt ID, session ID.
- Tool name, tool type, risk level.
- Status: running, success, failed, blocked.
- Sanitized input summary, output summary, error summary.
- Approval linkage if present.

Empty states:
- No global records: `No tool calls recorded yet.`
- No filtered records: `No tool calls under this filter.`

Abnormal states:
- Failed: red row with sanitized error.
- Blocked by policy: yellow row with policy code.
- Missing `sanitized === true`: render `[Raw output redacted for safety]` and do not show raw JSON.

Click actions:
- Filter by employee, task, trace, attempt, session.
- Row click: open sanitized detail modal from the already hydrated `/v1/tool-calls` list row. There is no MVP `/v1/tool-calls/{tool_call_id}` detail endpoint and the frontend must not invent one.
- `Open Task`: opens task drawer.
- `Open Trace`: calls `GET /v1/traces/{trace_id}/timeline`.

API:
- `GET /v1/tool-calls?limit=200`
- Filter parameters: `employee_id`, `task_id`, `trace_id`, `attempt_id`, `session_id`

Rendering rules:
- List cap is 200.
- Escape all HTML.
- Never render raw stdout/stderr or unredacted `input_json`/`output_json`.
- Detail modal uses client-side hydrated row state only; if the row lacks sanitized detail fields, show the redacted fallback instead of requesting a non-existent detail API.

## 9. Budget Panel

Purpose: show ledger-owned cost, not frontend-estimated cost.

Displayed fields:
- Total amount grouped by currency.
- Token input/output if backend returns them.
- Runtime seconds.
- Cost by employee, task, and type using simple tables/progress bars.
- Budget limit status and soft/hard limits.
- Recent budget events.

Empty states:
- `No budget ledger events recorded.`

Abnormal states:
- Soft limit exceeded: amber warning.
- Hard limit exceeded: red warning and disable paid run/retry/reassign actions.
- Runtime activity with zero budget: yellow `Cost missing` indicator.
- Mixed currencies: display one row per currency. Do not convert.

Click actions:
- Click employee/task row: filter other panels.
- Click budget event: open related task drawer.

API:
- `GET /v1/budget-summary`
- `GET /v1/budget-events?limit=50`
- Filtered variants with `employee_id`, `task_id`, `trace_id`, `attempt_id`.

Mixed currency rule:
- If `budget_summary.currency === "mixed"`, show a warning icon and the text `Mixed currencies: totals are ledger sums, not converted values.`
- In mixed mode, the primary visual must be per-currency ledger rows. Do not present the numeric `total_amount` as a single comparable money value.

## 10. Evidence Panel

Purpose: show what was actually delivered.

Displayed fields:
- Evidence ID, created time.
- Task ID, attempt ID, employee ID, trace ID.
- Evidence type, summary, checksum.
- Safe relative display path and basename.
- Final evidence flag.
- Completion contract reason when task-bound.

Empty states:
- `No evidence submitted yet.`

Abnormal states:
- Unsafe path: red row, preview disabled.
- Missing file: yellow row, preview disabled.
- Not final: neutral row; not sufficient for task done.

Click actions:
- `Preview Content`: call `GET /v1/evidence/{evidence_id}/content`.
- `Open Task`: open task drawer.
- `Open Trace`: open timeline.

API:
- `GET /v1/evidence?limit=50`
- `GET /v1/evidence?task_id={task_id}`
- `GET /v1/evidence/{evidence_id}/content`

Filtering rule:
- `/v1/evidence` currently supports `task_id` and `limit`; employee-card evidence filtering must be client-side over hydrated rows unless the backend later adds `employee_id`.

Security rules:
- Preview only through safe content API.
- Render plain text in read-only modal.
- Do not create `file://` links.
- Do not expose absolute `/Users/...` paths.

## 11. Task Detail Drawer

Purpose: single source of truth for a task lifecycle.

Open behavior:
- Click any task card or task link.
- Fetch exactly one primary call: `GET /v1/tasks/{task_id}`.
- Do not issue separate N+1 calls for attempts/tool calls/budget/evidence unless the task detail payload lacks a section.

Displayed sections:
- Task metadata: title, status, priority, source, target, trace ID.
- Completion contract: valid, reason, final evidence count.
- Attempts: attempt ID, employee, adapter/runtime, status, started/finished, last heartbeat, last progress, cancel flag.
- Runtime sessions: session ID, runtime type, status, PID if safe, started/stopped, last heartbeat/progress.
- Tool calls: sanitized list grouped by attempt/session.
- Budget: summary and recent events.
- Evidence: final evidence, checksum, preview buttons.
- Timeline: chronological items from task detail payload; if absent, optional `GET /v1/traces/{trace_id}/timeline`.
- Controls: correction, cancel, retry, reassign, reject/reopen.

Empty states:
- No attempts: `Task submitted, waiting for claim/run.`
- No runtime session: `No runtime session recorded yet.`
- No tool calls: `No tools used yet.`
- No budget: `No cost recorded yet.`
- No evidence: `No evidence submitted yet.`

Abnormal states:
- Stagnant attempt: amber attempt row and owner action hint.
- Failed/blocked tool call: expanded sanitized error summary.
- Hard budget limit: red sticky warning; disable paid rerun actions.
- Done without valid final evidence: red banner, show `Reject / Reopen`.

Control API bodies:

```json
{
  "correct": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/correct",
    "body": {"attempt_id": "{current_attempt_id}", "by": "owner", "message": "{owner_text}"}
  },
  "cancel": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/cancel",
    "body": {"attempt_id": "{current_attempt_id}", "by": "owner", "reason": "{owner_reason}"}
  },
  "retry": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/retry",
    "body": {"by": "owner", "reason": "{owner_reason}"}
  },
  "reassign": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/reassign",
    "body": {"by": "owner", "to": "{employee_id}", "reason": "{owner_reason}"}
  },
  "reject_reopen": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/reopen",
    "body": {"by": "owner", "reason": "{owner_reason}", "status": "submitted"}
  }
}
```

## 12. Three-Day Build Plan

### Day 1: Real Cockpit Data and Layout

Deliver:
- High-density dashboard layout with health bar, CEO summary, employee cards, running task cards.
- Poll `/v1/dashboard/cockpit` every 8 seconds.
- Show `/v1/doctor` banner/modal.
- Show skill-only employees without chat buttons.
- Show candidate employees as candidate, not active.

Verification:
- `curl -s http://127.0.0.1:8780/v1/dashboard/cockpit | python3 -m json.tool`
- `curl -s http://127.0.0.1:8780/v1/doctor | python3 -m json.tool`
- Browser: open `http://127.0.0.1:8780/dashboard.html` and confirm values are not static placeholders.

### Day 2: Task Drawer, Runtime Sessions, and Tool Calls

Deliver:
- Task Detail Drawer using single `GET /v1/tasks/{task_id}`.
- Runtime Sessions visible in home and drawer.
- Tool Calls panel with max 200 rows and filters.
- Correction/cancel/retry/reassign/reopen controls wired to existing APIs.

Verification:
- `curl -s http://127.0.0.1:8780/v1/tool-calls?limit=200 | python3 -m json.tool`
- `curl -s http://127.0.0.1:8780/v1/runtime-sessions | python3 -m json.tool`
- `curl -s http://127.0.0.1:8780/v1/tasks/{known_task_id} | python3 -m json.tool`
- Browser: open a real task from the latest local smoke and confirm attempt/session/tool/budget/evidence sections render.

### Day 3: Budget, Evidence Safety, and Local Closed-Loop Verification

Deliver:
- Budget panel from `budget-summary` and `budget-events`.
- Evidence panel and safe preview modal.
- Defensive rendering for unsanitized tool calls.
- Local skill worker smoke creates visible task/evidence/tool/budget data.

Verification:
- `curl -s http://127.0.0.1:8780/v1/budget-summary | python3 -m json.tool`
- `curl -s http://127.0.0.1:8780/v1/evidence | python3 -m json.tool`
- `bin/company-local-smoke --json-only --agents codex --direct-targets codex --reply-timeout 30 --skill-closed-loop --skill-timeout 60`
- `python3 -m unittest discover -s tests -p 'test*.py'`
- `bin/companyctl doctor --summary`

## 13. MVP Acceptance Criteria

The MVP is accepted only if all items below are true in the local environment:

1. CEO home uses live API data from `/v1/dashboard/cockpit`; no static fake runtime truth.
2. Every employee card shows readiness badge and does not confuse online with active.
3. Skill workers hide chat/direct actions but show task execution and evidence state.
4. Running task cards show latest progress, current attempt, stagnant/blocked/failed/done state, and final evidence validity.
5. Task drawer opens from real tasks and shows attempts, runtime sessions, tool calls, budget, evidence, and timeline.
6. Tool Calls panel renders only sanitized summaries and redacts unsafe/unknown records.
7. Budget panel displays ledger values by currency without frontend conversion.
8. Evidence preview only uses `/v1/evidence/{evidence_id}/content`.
9. Correction, cancel, retry, reassign, and reject/reopen actions call existing APIs with `by="owner"`.
10. `python3 -m unittest discover -s tests -p 'test*.py'` passes.
11. `bin/companyctl doctor --summary` is green or any remaining issue is classified with exact source and non-business impact.
12. Browser verification against `http://127.0.0.1:8780/dashboard.html` confirms the latest local skill closed-loop task is visible with attempt/session/tool/budget/evidence.

## 14. Antigravity Review Decisions

Round 1 design review was used as critique input only. Product decisions after review:

1. Task acceptance: MVP does not add a new task accept endpoint. Done plus valid final evidence is completion; failed owner review uses existing `POST /v1/tasks/{task_id}/reopen`.
2. Currency: no frontend token-to-USD conversion. Mixed currencies are displayed as separate ledger rows.
3. Sessions: no kill or archive session action in MVP. Cancel task/attempt is the supported control. Backend/session lifecycle cleanup is a later backend issue, not a 3-day UI action.
4. Doctor: browser shows `/v1/doctor`, never links to local shell command.
5. Drawer loading: use one task detail endpoint first to avoid N+1 polling.
