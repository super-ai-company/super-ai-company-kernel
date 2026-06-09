# CEO Cockpit 3-Day MVP UI Contract

Date: 2026-06-09

Scope: local Super AI Company Kernel CEO Cockpit MVP. This is a UI and API implementation contract for the next 3 development days. It intentionally does not design marketplace, WorkGraph canvas, skill pricing, distributed rental, payments, public users, or complex multi-tenant features.

## 0. Three-Day MVP Boundary

This document designs only the 3-day CEO Cockpit UI contract. It is not a product expansion plan.

In scope:
- Budget Center MVP: ledger totals, per-currency rows, task/employee cost rows, budget events.
- Tool Call automatic visibility: sanitized tool-call rows, status, risk, task/attempt/session linkage.
- Runtime Session visibility: session id, runtime type, heartbeat/progress freshness, current task/attempt.
- Dashboard truth surface: what each employee is doing, which tools were used, what it cost, and what evidence was submitted.

Out of scope:
- Marketplace.
- WorkGraph large canvas.
- Skill pricing.
- Distributed rental.
- Complex multi-tenant user/account management.

Backend gap rule: if the API does not provide a trusted field, the frontend must show `API gap` or an empty state. It must not infer business truth from chat, stdout, inbox files, ACK, or heartbeat alone.

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
| Task list | `GET /v1/tasks` | User-triggered lazy load only. Do not poll this endpoint in the main 8-second cockpit loop. |
| Task detail drawer | `GET /v1/tasks/{task_id}` | Single call. It already aggregates attempts, runtime sessions, tool calls, budget, evidence, completion contract, and timeline. Avoid N+1 drawer fetches. |
| Trace timeline | `GET /v1/traces/{trace_id}/timeline` | Use only when drawer needs richer trace story than task detail payload. The endpoint must return a sorted flat event list for MVP rendering. |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=&limit=` | No global Runtime panel in MVP. Show current session on employee/task cards and full task-bound sessions inside the task drawer. |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&trace_id=&attempt_id=&session_id=&limit=200` | Max list cap is 200. Render only sanitized summaries capped to 500 characters per summary field. |
| Budget summary | `GET /v1/budget-summary?employee_id=&task_id=&trace_id=&attempt_id=` | Frontend displays ledger values only; no currency conversion. |
| Budget events | `GET /v1/budget-events?...` | Recent spend rows. |
| Evidence list | `GET /v1/evidence?task_id=&employee_id=&limit=` | Evidence panel and employee-bound evidence history. |
| Evidence preview | `GET /v1/evidence/{evidence_id}/safe-preview` | Only safe content preview. No direct file path reads. `/content` remains compatibility-only. |
| Correction | `POST /v1/tasks/{task_id}/correct` | Body: `attempt_id`, `by="owner-shift"`, `message`. |
| Cancel | `POST /v1/tasks/{task_id}/cancel` | Body: `attempt_id`, `by="owner-shift"`, `reason`. No kill-session button in MVP. |
| Retry | `POST /v1/tasks/{task_id}/retry` | Body: `by="owner-shift"`, `reason`. |
| Reassign | `POST /v1/tasks/{task_id}/reassign` | Body: `by="owner-shift"`, `to`, `reason`. |
| Reject evidence / reopen | `POST /v1/tasks/{task_id}/reopen` | Body: `by="owner-shift"`, `reason`, `status="submitted"`. MVP has reject/reopen, not a new task-accept endpoint. |

### 2.1 Required API Gaps Before Final UI Wiring

These are not frontend workarounds. They are backend/API requirements that must be implemented or explicitly shown as unavailable:

| Gap | Required backend contract | Frontend fallback until implemented |
|---|---|---|
| Mixed-currency budget | `/v1/budget-summary` must return per-currency totals, and preferably per-currency `by_employee` and `by_task` rows. Example: `total_amounts_by_currency: {"USD": 1.50, "THB": 120.00}`. | Show mixed-currency warning and per-currency rows only when provided. Do not show `total_amount` as a comparable money value. |
| Doctor health in cockpit | `/v1/dashboard/cockpit` should include `doctor.ok`, `doctor.issue_count`, `doctor.exit_code`, and `doctor.generated_at` to avoid double polling during the 8-second refresh loop. | Poll `/v1/doctor` separately and show a slower diagnostics banner. |
| Completion invalid marker | `long_tasks[]`, task cards, and `GET /v1/tasks/{task_id}` should include `completion_invalid: true/false` and `completion_invalid_reason`. | Show invalid count if present, but do not guess which task is invalid from unrelated evidence counts. |
| Tool-call detail payload size | `/v1/tool-calls?limit=200` must return only summarized and sanitized fields. `input_summary`, `output_summary`, and `error_message` must each be capped to 500 display characters. | Row click opens only the hydrated sanitized summary row; no invented detail endpoint and no raw stdout/stderr. |
| Employee evidence filter | `/v1/evidence` supports `employee_id`; employee evidence panels must use the backend filter instead of local hydrated-row guessing. | If the API is unavailable, show an explicit API gap message and do not claim full employee evidence history. |
| Direct message action | Direct message is not part of the 3-day UI MVP. | Hide all direct message/chat controls. Never use DM success as readiness or evidence. |
| Employee current task title | `GET /v1/employees` should include `current_task_title` or the cockpit aggregate should hydrate it from the active task card. | If absent, show task id plus `Title unavailable from API`; do not invent titles. |
| Active-limited reason | `GET /v1/dashboard/cockpit` should include `active_limited_reasons` keyed by employee id. | If absent, show `Limited reason unavailable from API`. |
| Stagnant threshold | `GET /v1/tasks/{task_id}` should include `stagnant_threshold_minutes` when task-specific thresholds exist. | If absent, show backend state label only; do not compute failure from browser time. |
| Burn rate | `/v1/budget-summary` may include `burn_rate_per_hour_by_currency`. | If absent, show `Burn rate unavailable`; do not estimate from frontend. |

## 3. Global UI Rules

1. Poll `/v1/dashboard/cockpit` every 8 seconds. No WebSocket and no SSE listener in the 3-day MVP, even if the backend exposes an SSE route.
2. If API fails, keep last successful data visible but dimmed and show `Offline: API Connection Lost`.
   - Retry policy: keep the next 3 retries at the normal 8-second interval, then back off to one retry every 30 seconds until the API recovers.
   - Do not queue overlapping requests; skip a poll tick if the previous poll is still in flight.
3. If `/v1/doctor` returns non-zero `exit_code`, show a yellow diagnostics banner with `ok`, `issue_count`, and latest timestamp. Debug mode may show raw JSON in a plain `<pre>` block; do not build a JSON tree renderer. Poll `/v1/doctor` slowly, at most once per 60 seconds, unless `/v1/dashboard/cockpit.doctor.ok === false`.
   - If `/v1/dashboard/cockpit` already includes a fresh `doctor` aggregate, do not run separate `/v1/doctor` polling during normal refresh.
4. Do not render raw local absolute paths. Display safe relative paths and basenames only.
5. Do not calculate prices, exchange rates, or token-to-USD conversion in frontend. Display backend ledger totals. If multiple currencies exist, display per-currency rows, not a fake combined total.
6. Do not add `Kill Session`, `Archive Stale Sessions`, marketplace actions, skill pricing, or user/account management in this MVP.
7. Skill-only or `task_unsupported` workers must hide chat/direct buttons, but still show task progress, tool calls, budget, artifacts, and evidence.
8. Candidate employees must not be visually promoted to active. Use explicit badges: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
9. Stale/stagnant/running labels are backend-owned. The frontend may display relative time labels, but must not decide task failure from local browser time.
10. For every action button, the default confirmation modal must use human-readable business copy. API path and actor `owner-shift` appear only in developer/debug mode.
11. Filters are single-field quick filters only. Do not implement compound filtering across Tool Calls, Budget, and Evidence in the 3-day MVP.
12. `GET /v1/agent-matrix` output is summarized as the final readiness badge and a short reason. Do not render a detailed matrix checklist or scoring UI.
13. Use plain HTML/CSS/vanilla JavaScript in the existing dashboard template. Do not introduce large UI libraries, chart libraries, graph libraries, D3, Mermaid, Gantt, or canvas packages.
14. `GET /v1/tasks` is lazy-loaded only after user interaction such as clicking `View all tasks`; it is not part of the normal cockpit polling loop.

## 4. Page Layout

One page, high-density operations console:

1. Top health bar: API state, doctor state, generated time, active sessions, active attempts, pending approvals.
2. CEO summary grid.
3. Employee cards.
4. Running/stagnant/blocked task cards.
5. Three ledger panels: Tool Calls, Budget, Evidence.
6. Right-side Task Detail Drawer.

Do not introduce complex charts or a global Runtime Session table. Use CSS grid, tables, small progress bars, and severity badges. Runtime Session visibility belongs on employee cards, running task cards, and the task detail drawer.

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
- Doctor: `doctor.ok`, `doctor.issue_count`, `doctor.exit_code` if included by `/v1/dashboard/cockpit`; otherwise show `/v1/doctor` as separately polled.
  If cockpit already includes fresh doctor fields, do not poll `/v1/doctor` separately.

Empty states:
- No employees: `No AI employees registered.`
- No active tasks: `No running tasks.`
- No budget: `No budget ledger events recorded.`
- No evidence: `No evidence submitted yet.`

Abnormal states:
- API offline: red banner, stale data dimmed.
- Doctor unhealthy: yellow banner, click opens `/v1/doctor` JSON modal.
- Hard budget limit: red banner; disable retry/reassign actions that would create more paid execution, but keep cancel/correction/reopen visible.
- Hard budget control copy must use exactly: disable `Retry` and `Reassign`; keep `Cancel`, `Correct`, and `Reject / Reopen`.
- Completion invalid: red count and filter to invalid task cards.
- API gap: gray banner such as `Completion invalid task list unavailable from cockpit API.` Do not guess.

Click actions:
- Employee count click: filter Employee Cards by badge/status.
- Stagnant/blocked count click: filter Task Cards.
- Budget click: scroll to Budget Panel.
- Evidence click: scroll to Evidence Panel.
- Doctor banner click: fetch and show `/v1/doctor`.

API:
- Primary: `GET /v1/dashboard/cockpit`
- Diagnostic detail: `GET /v1/doctor`

Minimum layout:
- Left column: CEO summary grid and employee readiness cards.
- Center column: running/stagnant/blocked task cards sorted by owner attention priority.
- Right column: Tool Calls, Budget, and Evidence compact panels.
- Task detail drawer slides from the right and must not replace the cockpit overview.

Required owner-visible headline copy:
- `Who is working`: active employees, candidate employees, skill-only workers, unsafe workers.
- `What is running`: current task, attempt, session, latest progress, stagnant/blocker state.
- `What was used`: latest tool calls with risk/status.
- `What it cost`: ledger totals and recent spend events.
- `What was delivered`: final evidence and invalid completion markers.

## 6. Employee Cards

Purpose: make employee readiness honest. Online is not active.

Displayed fields:
- Employee ID, display name, role, runtime/adapter type.
- Readiness badge.
- Status: online, busy, candidate, active-limited, abnormal, unsafe.
- Heartbeat: last seen and freshness label.
- Current work: task id, attempt id, session id, latest progress.
- Current task title if available; otherwise show task id and `Title unavailable from API`.
- Capabilities: task/chat/tool support summary.
- Ledger rollup: tool call count, evidence count, cost by currency.
- Red/yellow/green indicators: high-risk tool activity, failed tool calls, budget warning, final evidence present.

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
- `Filter Logs`: apply employee filter to Tool Calls/Budget panels, and apply local evidence filtering only across hydrated evidence rows unless `/v1/evidence?employee_id=` exists.
- `Filter Evidence`: call `/v1/evidence?employee_id={employee_id}` and show only backend-filtered evidence rows.
- `Verify Runtime Evidence`: call `GET /v1/agent-matrix?agents={employee_id}` and show only readiness badge plus one short backend reason. Do not render attendance/direct/runtime/task/progress/evidence/stale as a checklist or matrix table.

API:
- `GET /v1/employees`
- `GET /v1/employees/{employee_id}`
- `GET /v1/agent-matrix?agents={employee_id}`
- Optional filters: `/v1/tool-calls?employee_id=...`, `/v1/budget-summary?employee_id=...`; evidence supports only `/v1/evidence?task_id=...` in current MVP unless backend adds `employee_id`.

Direct Message exclusion:
- No chat bubbles, no receipt timeline, and no DM inbox in the 3-day MVP.
- Do not render `Send Direct Message` in normal or developer mode during this MVP.
- Owner-to-worker guidance must use task-bound correction through `POST /v1/tasks/{task_id}/correct`, not private chat.
- DM success, inbox files, greetings, and ACK messages never change readiness, task status, evidence status, or budget status.

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
- Completion invalid marker: backend `completion_invalid` and `completion_invalid_reason` when available.
- Cost summary and evidence count.
- Red/yellow/green indicators: high-risk tool calls, failed tool calls, over-budget, stagnant, valid final evidence.

Empty states:
- No running tasks: `No running tasks.`
- Submitted but unclaimed: `Awaiting employee claim.`

Abnormal states:
- Stagnant: `Employee is still online, but no new progress for 15 minutes. Continue waiting, send probe, request Hermes correction, or cancel.`
- Blocked: show blocker reason.
- Failed: show failure reason and retry/reassign controls.
- Done without final evidence: red `Completion Invalid`.
- Missing completion marker: neutral `Completion API gap`; do not infer from count-only data.

Click actions:
- Click card: open Task Detail Drawer via `GET /v1/tasks/{task_id}`.
- `Send Correction`: POST `/v1/tasks/{task_id}/correct` with current `attempt_id`, `by="owner-shift"`, user message.
- `Cancel Attempt`: POST `/v1/tasks/{task_id}/cancel` with current `attempt_id`, `by="owner-shift"`, reason.
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
- No pagination, infinite scroll, or load-more controls in MVP; render only the latest 200 rows returned by the backend.
- `input_summary`, `output_summary`, and `error_message` are display summaries capped to 500 characters each.
- Escape all HTML.
- Never render raw stdout/stderr or unredacted `input_json`/`output_json`.
- Detail modal uses client-side hydrated row state only; if the row lacks sanitized detail fields, show the redacted fallback instead of requesting a non-existent detail API.
- If `sanitized !== true` or required summary fields are missing, the detail modal must show `[Raw output redacted for safety]` and disable copy buttons.
- Do not use virtual terminal output, transcript blobs, or full command logs in this panel.

Automatic recording requirement:
- The panel is useful only if adapters write tool-call rows automatically.
- Codex, Agy, OpenClaw bridge, local scripts, and Docker skills must record shell/API/browser/file/model operations through the Kernel.
- If a running attempt has progress but zero tool calls, show `Tool-call ledger missing for this attempt` instead of implying no tools were used.

## 9. Budget Panel

Purpose: show ledger-owned cost, not frontend-estimated cost.

Displayed fields:
- Total amount grouped by currency.
- Per-currency totals when the backend returns `total_amounts_by_currency` or equivalent ledger rows.
- Token input/output if backend returns them.
- Runtime seconds.
- Cost by employee, task, and type using simple tables/progress bars.
- Budget limit status and soft/hard limits.
- Recent budget events.

Empty states:
- `No budget ledger events recorded.`

Abnormal states:
- Soft limit exceeded: amber warning.
- Hard limit exceeded: red warning and disable `Retry`, `Reassign`, and any future paid run action. Keep `Cancel`, `Correct`, and `Reject / Reopen` visible.
- Runtime activity with zero budget: yellow `Cost missing` indicator.
- Mixed currencies: display one row per currency. Do not convert.
- Burn rate unavailable: neutral `Burn rate unavailable from API`; do not estimate from frontend.

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
- If the backend does not return per-currency totals, show `API gap: per-currency budget totals unavailable` instead of fabricating rows.
- The frontend must not sum `budget-events` into per-currency totals. Aggregation belongs to `/v1/budget-summary`.

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
- `Preview Content`: call `GET /v1/evidence/{evidence_id}/safe-preview`.
- `Open Task`: open task drawer.
- `Open Trace`: open timeline.

API:
- `GET /v1/evidence?limit=50`
- `GET /v1/evidence?task_id={task_id}`
- `GET /v1/evidence/{evidence_id}/safe-preview`

Filtering rule:
- `/v1/evidence` supports `task_id`, `employee_id`, and `limit`; employee-card evidence filtering must use the backend `employee_id` filter.

Security rules:
- Preview only through safe content API.
- Render only plain text, JSON text, or Markdown source in a read-only `<pre>` modal.
- If preview text exceeds 1 MB, block or truncate preview and show `Preview too large for MVP safe viewer`.
- For image, PDF, Word, binary, HTML, or unknown content types, show `Preview unavailable for this format in MVP` and do not add a renderer.
- Do not create `file://` links.
- Do not expose absolute `/Users/...` paths.
- Do not use `/v1/evidence/{evidence_id}/content` for new UI wiring; it exists only as backward-compatible alias behavior.

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
- Timeline: chronological text-only vertical list from task detail payload; if absent, optional `GET /v1/traces/{trace_id}/timeline`.
- Controls: correction, cancel, retry, reassign, reject/reopen.

Empty states:
- No attempts: `Task submitted, waiting for claim/run.`
- No runtime session: `No runtime session recorded yet.`
- No tool calls: `No tools used yet.`
- No budget: `No cost recorded yet.`
- No evidence: `No evidence submitted yet.`
- New submitted task: collapse Tool Calls, Budget, and Evidence by default until at least one row exists.

Abnormal states:
- Stagnant attempt: amber attempt row and owner action hint.
- Failed/blocked tool call: expanded sanitized error summary.
- Hard budget limit: red sticky warning; disable `Retry` and `Reassign`.
- Hard budget limit button policy: disable `Retry` and `Reassign`; keep `Cancel`, `Correct`, and `Reject / Reopen` available because they do not create new paid execution by themselves.
- Done without valid final evidence: red banner, show `Reject / Reopen`.
- Timeline render attempts to become graph/tree/Gantt: not allowed in MVP; keep the vertical text event stream.
- If `GET /v1/traces/{trace_id}/timeline` returns nested spans instead of a sorted flat event list, show `Timeline API gap: flat event list unavailable` and do not parse it into a graph/tree.

Control API bodies:

MVP actor rule: `by: "owner-shift"` is a local single-owner default. Do not build dynamic user identity, login, RBAC, or multi-tenant ownership in this 3-day MVP.

```json
{
  "correct": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/correct",
    "body": {"attempt_id": "{current_attempt_id}", "by": "owner-shift", "message": "{owner_text}"}
  },
  "cancel": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/cancel",
    "body": {"attempt_id": "{current_attempt_id}", "by": "owner-shift", "reason": "{owner_reason}"}
  },
  "retry": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/retry",
    "body": {"by": "owner-shift", "reason": "{owner_reason}"}
  },
  "reassign": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/reassign",
    "body": {"by": "owner-shift", "to": "{employee_id}", "reason": "{owner_reason}"}
  },
  "reject_reopen": {
    "method": "POST",
    "path": "/v1/tasks/{task_id}/reopen",
    "body": {"by": "owner-shift", "reason": "{owner_reason}", "status": "submitted"}
  }
}
```

## 12. False-State Guardrails

These guardrails prevent a beautiful but misleading cockpit. If a trusted backend field is missing, the UI must show a gap or a bounded empty state instead of guessing.

1. Employee evidence filtering:
   - Risk: the evidence API may be offline or return a backend error.
   - UI rule: employee evidence count is trusted only when returned by `/v1/evidence?employee_id={employee_id}` or from a task-bound drawer payload. Do not fall back to local global-row guessing.
   - Copy: `Employee evidence history unavailable from API.`
2. Current task title:
   - Risk: employee cards may have `task_id` but no title.
   - UI rule: show the task id and `Title unavailable from API`; do not infer from old messages or inbox files.
3. Stagnant/running state:
   - Risk: browser time can drift and backend stagnant checks can lag.
   - UI rule: backend state wins. Browser may show relative age but cannot decide `failed`, `stale`, or `stagnant`.
4. Tool-call sanitization:
   - Risk: raw stdout/stderr or tool payloads can contain secrets.
   - UI rule: if a row lacks sanitized summaries or safe display fields, render `[Raw output redacted for safety]` and disable copy controls.
5. Mixed-currency budget:
   - Risk: a single `total_amount` can look like comparable money when currencies differ.
   - UI rule: show per-currency rows only. If missing, show `API gap: per-currency budget totals unavailable`. Never aggregate `budget-events` in frontend to invent totals.
6. Completion evidence:
   - Risk: `done` without final evidence looks complete.
   - UI rule: render `Completion Invalid` unless backend says final evidence is valid for the same `task_id` and current/final attempt context.
7. Skill worker chat:
   - Risk: skill workers such as `ecommerce-copy-skill` can execute but cannot chat.
   - UI rule: hide chat/direct UI, but keep progress, runtime, tool calls, budget, artifacts, and evidence visible.

## 13. Worker Reporting Contract For UI Truth

The cockpit is only as good as the ledger events workers submit. Codex, Agy, OpenClaw bridge workers, local scripts, and Docker skills must follow this minimum reporting contract before their work can appear as trustworthy UI state:

1. Heartbeat:
   - A running runtime session must update heartbeat at the backend-defined interval.
   - Heartbeat proves liveness only; it never proves progress or completion.
2. Incremental progress:
   - Long tasks must emit task-bound progress with `task_id`, `attempt_id`, `employee_id`, `status`, `message`, and timestamp.
   - UI expects fresh progress at least before the backend stagnant threshold.
3. Tool-call records:
   - Every meaningful command/API/browser/file/tool action must create a tool-call row with sanitized summaries, status, risk level, and task/attempt/session linkage.
   - Failed or blocked calls must record a concrete error or policy reason.
4. Budget records:
   - Paid or metered work must write budget events with provider/model when available, token input/output when available, runtime seconds, amount, currency, and task/attempt/employee linkage.
   - No frontend cost estimation is allowed as a substitute.
5. Correction ack:
   - When owner/Hermes sends `POST /v1/tasks/{task_id}/correct`, the worker must acknowledge through an event/progress update and then submit follow-up progress.
   - If it cannot comply, it must block with a reason.
6. Cancel ack:
   - When a task/attempt is cancelled, the worker must stop producing final evidence for that attempt and record cancelled/blocked state.
   - Old cancelled attempt outputs cannot be promoted as current completion evidence.
7. Final evidence:
   - Completion requires final evidence bound to the same `task_id` and relevant `attempt_id`.
   - Evidence must use safe workspace/artifact/evidence/final paths and include checksum and summary.

## 14. Three-Day Build Plan

### Day 1: Real Cockpit Data and Layout

Deliver:
- High-density dashboard layout with health bar, CEO summary, employee cards, running task cards.
- Poll `/v1/dashboard/cockpit` every 8 seconds.
- Show `/v1/doctor` status banner; debug mode may show raw JSON in a plain `<pre>`.
- Show skill-only employees without chat buttons.
- Show candidate employees as candidate, not active.
- Show API gap banners for missing doctor-in-cockpit and missing completion-invalid task markers.

Verification:
- `curl -s http://127.0.0.1:8765/v1/dashboard/cockpit | python3 -m json.tool`
- `curl -s http://127.0.0.1:8765/v1/doctor | python3 -m json.tool`
- Browser: open `http://127.0.0.1:8780/dashboard.html` and confirm values are not static placeholders.

### Day 2: Task Drawer, Runtime Sessions, and Tool Calls

Deliver:
- Task Detail Drawer using single `GET /v1/tasks/{task_id}`.
- Runtime Sessions visible on employee/task cards and in drawer; no global Runtime table.
- Tool Calls panel with max 200 rows and filters.
- Correction/cancel/retry/reassign/reopen controls wired to existing APIs.
- Direct Message/chat controls are completely hidden. Skill workers never show chat/direct actions.

Verification:
- `curl -s http://127.0.0.1:8765/v1/tool-calls?limit=200 | python3 -m json.tool`
- `curl -s http://127.0.0.1:8765/v1/runtime-sessions | python3 -m json.tool`
- `curl -s http://127.0.0.1:8765/v1/tasks/{known_task_id} | python3 -m json.tool`
- Browser: open a real task from the latest local smoke and confirm attempt/session/tool/budget/evidence sections render.

### Day 3: Budget, Evidence Safety, and Local Closed-Loop Verification

Deliver:
- Budget panel from `budget-summary` and `budget-events`.
- Evidence panel and plain-text-only safe preview modal.
- Defensive rendering for unsanitized tool calls.
- Local skill worker smoke creates visible task/evidence/tool/budget data.
- Mixed-currency display uses backend per-currency rows only; no frontend conversion or fake combined amount.

Verification:
- `curl -s http://127.0.0.1:8765/v1/budget-summary | python3 -m json.tool`
- `curl -s http://127.0.0.1:8765/v1/evidence | python3 -m json.tool`
- `bin/company-local-smoke --json-only --agents codex --direct-targets codex --reply-timeout 30 --skill-closed-loop --skill-timeout 60`
- `python3 -m unittest discover -s tests -p 'test*.py'`
- `bin/companyctl doctor --summary`

## 15. MVP Acceptance Criteria

The MVP is accepted only if all items below are true in the local environment:

1. CEO home uses live API data from `/v1/dashboard/cockpit`; no static fake runtime truth.
2. Every employee card shows readiness badge and does not confuse online with active.
3. Skill workers hide chat/direct actions but show task execution and evidence state.
4. Running task cards show latest progress, current attempt, stagnant/blocked/failed/done state, and final evidence validity.
5. Task drawer opens from real tasks and shows attempts, runtime sessions, tool calls, budget, evidence, and timeline.
6. Tool Calls panel renders only sanitized summaries and redacts unsafe/unknown records.
7. Budget panel displays ledger values by currency without frontend conversion.
8. Evidence preview only uses `/v1/evidence/{evidence_id}/safe-preview` and renders text/JSON/Markdown source in `<pre>` only.
9. Correction, cancel, retry, reassign, and reject/reopen actions call existing APIs with `by="owner-shift"`.
10. `python3 -m unittest discover -s tests -p 'test*.py'` passes.
11. `bin/companyctl doctor --summary` is green or any remaining issue is classified with exact source and non-business impact.
12. Browser verification against `http://127.0.0.1:8780/dashboard.html` confirms the latest local skill closed-loop task is visible with attempt/session/tool/budget/evidence.
13. Runtime Session state is visible without adding a separate global Runtime Session panel.
14. Direct message does not become a chat product; it is hidden completely in the 3-day MVP.
15. Timeline is a vertical text event list, not a graph, tree, canvas, or Gantt view.
16. No compound filtering, graph/canvas/Gantt library, SSE/WebSocket listener, frontend cost estimation, or large UI component library is added.

## 16. Antigravity Multi-Round Review Log

Antigravity was used as a design-review employee only. Its replies are critique input, not execution evidence. Code changes, tests, and file updates remain Codex-owned unless a future Agy task writes files and submits structured evidence.

### Round 1: Coverage And False-State Risks

Agy confirmed the UI contract does not drift from the 3-day MVP and that the required areas are covered: CEO home, employee cards, running task cards, Tool Calls, Budget, Evidence, and task detail drawer. It identified five high-risk false states that must be represented in the contract:

1. Employee evidence filtering can show false empty states when `/v1/evidence` lacks `employee_id` and the loaded page is capped.
2. Employee cards can hide current work when the backend lacks `current_task_title`.
3. Stagnant state must stay backend-owned or browser time will mislabel long-running work.
4. Missing sanitized tool-call fields can turn useful supervision into redacted blanks.
5. Mixed-currency budget without per-currency rows can create misleading cost totals.

### Round 2: Scope Locks

Agy confirmed the proposed changes strengthen the MVP and do not conflict with the boss-facing goal. It recommended hard prohibitions to prevent scope drift:

1. Completely hide DM/chat controls in the 3-day MVP; use task-bound correction instead.
2. Simplify Doctor to status plus raw debug JSON, not a JSON tree renderer.
3. Forbid compound filtering across Tool Calls/Budget/Evidence.
4. Forbid detailed agent-matrix visualization; show readiness badge and short reason only.
5. Forbid graph/tree/Gantt/canvas/timeline libraries.
6. Forbid SSE/WebSocket listeners in the MVP; use 8-second REST polling only.
7. Forbid frontend cost estimation; show ledger data or `Cost unavailable`.
8. Forbid large UI component libraries; use existing template, vanilla CSS, and vanilla JavaScript.

## 17. Final Design Decisions

Multi-round review decisions:

1. Task acceptance: MVP does not add a new task accept endpoint. Done plus valid final evidence is completion; failed owner review uses existing `POST /v1/tasks/{task_id}/reopen`.
2. Currency: no frontend token-to-USD conversion. Mixed currencies are displayed as separate ledger rows.
3. Sessions: no kill or archive session action in MVP. Cancel task/attempt is the supported control. Backend/session lifecycle cleanup is a later backend issue, not a 3-day UI action.
4. Doctor: browser shows `/v1/doctor`, never links to local shell command.
5. Drawer loading: use one task detail endpoint first to avoid N+1 polling.
6. Backend gap handling: mixed-currency rows, completion-invalid task markers, doctor summary in cockpit, tool-call detail payload size, and direct-message API are explicit API contracts. The dashboard must show `API gap` instead of inventing state.
7. Stagnant state: browser renders backend-owned long-task state; it does not use local clock drift to decide failure.
8. Runtime sessions: no global Runtime panel in the MVP. Show runtime state where it explains current employee/task behavior.
9. Tool-call summaries: list/detail summaries are capped and sanitized; full raw logs are out of scope.
10. Evidence preview: text-only via safe API. No PDF/image/Word/HTML renderer in this 3-day MVP.
11. Timeline: plain vertical event list only. Graphical trace trees and WorkGraph canvas are explicitly out of scope.
12. Direct message: completely hidden in the 3-day MVP. Owner guidance uses task-bound correction only.
13. False-state guardrails: the dashboard must prefer `API gap`, `local view only`, or `unavailable` over inferred truths.
14. Worker reporting contract: heartbeat, progress, tool-call, budget, correction ack, cancel ack, and final evidence are required for trustworthy UI state.
15. Implementation stack: existing dashboard template plus vanilla JavaScript/CSS only; no large UI framework or graph/canvas dependency.
