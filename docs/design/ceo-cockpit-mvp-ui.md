# CEO Cockpit 3-Day MVP UI

Date: 2026-06-09

Source: `docs/research/ai-company-os-research-plan.md`, current Kernel API/schema scan, and three Antigravity design-review rounds.

Scope: this is a UI contract for the next 3-day MVP only. It must make real backend control-plane data visible. It must not expand into marketplace, WorkGraph canvas, skill pricing, distributed rental, multi-tenant SaaS, or chat UI.

## Goal

The CEO Cockpit must answer one question in one screen:

> What is each AI employee doing, what tools did it use, what did it cost, and what evidence did it submit?

Completion must not be inferred from chat, ACK, stdout, inbox files, or heartbeat. The UI can only show completion as valid when Kernel data proves task/attempt-bound final evidence.

## MVP Priorities

1. Budget Center MVP: show ledger-recorded spend, tokens, runtime seconds, provider/model, and per employee/task/attempt cost.
2. Tool Call automatic record visibility: show structured tool calls from Kernel, not raw stdout/stderr.
3. Runtime Session visibility: show which employee has which live session, task, attempt, heartbeat, and progress.
4. Dashboard truthfulness: show active/candidate/skill-only/unsafe employees differently.
5. Evidence safety: preview only through safe evidence APIs and never expose absolute local paths.

## Non-Goals

- No marketplace.
- No WorkGraph large canvas.
- No skill pricing or frontend price calculation.
- No distributed rental.
- No multi-tenant/RBAC system.
- No chat/direct-message buttons.
- No SSE/WebSocket in this MVP; use REST polling every 5-10 seconds.
- No raw stdout/stderr, `file://` links, absolute `/Users/...` paths, or browser download/open actions.
- No frontend USD/token estimation. Budget values must come from Kernel budget ledger.

## Layout

Use one high-density Cockpit page with a top status band and two-column body.

First screen must show:

- Health Bar.
- CEO Summary.
- Employee Cards.
- Running Task Cards.

Default folded or drawer-only:

- Tool Calls.
- Budget event details.
- Evidence list/details.
- Task Detail Drawer.

Reason: the owner first needs system trust, employee capacity, and active workload. Tool calls, budget events, and evidence are critical, but they are task context and should not drown the first screen.

## API Contract

All paths use `/v1/*`. If an endpoint or field is missing, the UI must show empty/disabled `[API Gap - Mocking Prohibited]` state. It must not fabricate employees, tasks, costs, tools, or evidence.

| Need | API | MVP rule |
|---|---|---|
| Cockpit aggregate | `GET /v1/dashboard/cockpit` | Primary data source for summary, employee cards, and running tasks. |
| Health | `GET /v1/doctor` | Show API/doctor state and issue count. |
| Employees | `GET /v1/employees` and `GET /v1/employees/{employee_id}` | Detail only on click. |
| Tasks | `GET /v1/tasks/{task_id}` | Opens drawer. |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=&limit=` | Read-only. |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&trace_id=&attempt_id=&session_id=&limit=200` | Sanitized rows only. |
| Budget summary | `GET /v1/budget-summary?employee_id=&task_id=&trace_id=&attempt_id=` | Read-only totals. |
| Budget events | `GET /v1/budget-events?employee_id=&task_id=&trace_id=&attempt_id=&limit=50` | Read-only ledger. |
| Evidence list | `GET /v1/evidence?task_id=&employee_id=&limit=50` | Safe metadata only. |
| Evidence preview | `GET /v1/evidence/{evidence_id}/safe-preview` | Only safe preview API for new UI. |
| Approvals | `GET /v1/approvals?status=pending` | Optional owner attention strip. |
| Correction | `POST /v1/tasks/{task_id}/correct` | Disabled if absent. |
| Cancel | `POST /v1/tasks/{task_id}/cancel` | Disabled if absent. |
| Retry/Reassign/Reopen | corresponding task control APIs | Visible disabled if absent. |

Write actions must disable immediately on click and stay disabled until success, error, or request timeout is rendered. Duplicate owner clicks must not create duplicate correction/cancel/retry/reassign/approval requests.

## 1. Health Bar

Display fields:

- API status.
- Doctor status and issue count.
- Last successful sync time.
- Poll interval.
- Last data age.

Empty state: `Waiting for Kernel API.`

Abnormal state:

- API offline: keep last successful data visible, dim it, and show `Offline: API Connection Lost`.
- Doctor unhealthy: yellow/red banner with issue count.
- Missing endpoint: `[API Gap - Mocking Prohibited]`.

Click actions:

- `Refresh Now`: re-fetch cockpit and visible dependent panels.
- Doctor banner: fetch `/v1/doctor` and show sanitized detail modal.

## 2. CEO Summary

Display fields:

- Employee counts by readiness: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
- Task counts: running, stagnant, blocked, failed, awaiting approval, done, completion invalid.
- Runtime counts: active sessions, stale sessions, active attempts.
- Tool counts: running, failed, blocked, missing ledger.
- Budget totals by currency, token input/output, runtime seconds, budget limit state.
- Evidence counts: final, unsafe, missing, invalid completion.

Empty state:

- `No employees registered.`
- `No running tasks.`
- `No budget ledger events recorded.`
- `No evidence submitted yet.`

Abnormal state:

- Hard budget exceeded: red summary card and disable retry/reassign.
- Done without valid final evidence: red `Completion Invalid`.
- Runtime progress exists but no tool call: yellow `Tool-call ledger missing`.

Click actions:

- Count cards filter Employee Cards, Running Tasks, Tool Calls, Budget, or Evidence.

## 3. Employee Cards

Display fields:

- Employee ID, display name, role.
- Runtime/adapter type.
- Readiness badge.
- Online/status and heartbeat freshness.
- Current task title, `task_id`, `attempt_id`, `session_id`.
- Latest progress and progress freshness.
- Runtime session count.
- Tool call count and failed/blocked tool count.
- Budget summary and token input/output if recorded.
- Evidence count and final evidence count.
- Capability text: `task execution`, `tool calls`, `evidence`, `no chat in MVP`.

Empty state:

- Active idle: `Idle. Awaiting task assignment.`
- Candidate: `Awaiting activation. Needs structured runtime/progress/evidence proof.`
- Skill worker: `No chat; task/evidence execution only.`

Abnormal state:

- `candidate_only`: gray dashed card, not counted as active capacity.
- `online_only`: yellow, `Online but no task/evidence proof.`
- `task_unsupported`: neutral skill/tool-worker badge; hide assignment/chat.
- `unsafe`: red; auto-assignment disabled.
- stale heartbeat: dim card.

Click actions:

- Card click: fetch employee detail.
- `View Task`: open Task Detail Drawer.
- `Filter Tools`: `/v1/tool-calls?employee_id=...`.
- `Filter Budget`: budget summary/events filtered by employee.
- `Filter Evidence`: `/v1/evidence?employee_id=...`.

Rules:

- Hide all chat/direct buttons.
- Do not visually promote candidate, online-only, no-reply, unsafe, or task-unsupported employees into active staff.
- `ecommerce-copy-skill`-style workers must show execution progress, artifacts, tool calls, budget, and evidence, but no chat action.

## 4. Running Task Cards

Display fields:

- Task ID, title, priority, status.
- Assigned employee.
- `trace_id`, current `attempt_id`, current `session_id`.
- Long-task state: running, progress fresh, stagnant, correcting, blocked, failed, cancelled, done.
- Elapsed runtime.
- Last heartbeat and last progress age.
- Latest progress message.
- Tool calls count and failed/blocked count.
- Budget summary by currency.
- Evidence count and final evidence status.
- Completion contract: valid/invalid and reason.

Empty state:

- `No running tasks.`
- `Awaiting employee claim.`

Abnormal state:

- Stagnant: `Employee is still online, but no new progress. Continue waiting, send correction, or cancel.`
- Claimed without runtime session: `Cold start: waiting for runtime session.`
- Progress but no tool calls: `Tool-call ledger missing for this attempt.`
- Done without final evidence: `Completion Invalid`.

Click actions:

- Card click: open Task Detail Drawer with `/v1/tasks/{task_id}`.
- Correct: `POST /v1/tasks/{task_id}/correct`.
- Cancel: `POST /v1/tasks/{task_id}/cancel`.
- Retry/Reassign/Reopen: visible disabled with API-gap label if unsupported.

## 5. Tool Calls Panel

Default location: collapsed panel under the first screen and tab inside Task Detail Drawer.

Display fields:

- Tool call ID.
- Employee ID, task ID, trace ID, attempt ID, session ID.
- Tool name and type.
- Status: planned, running, success, failed, blocked, cancelled.
- Risk level and approval ID.
- Started/finished time and duration.
- Sanitized input summary.
- Sanitized output or error summary.

Empty state:

- `No tool calls recorded yet.`
- `No tool calls under this filter.`

Abnormal state:

- Failed/blocked tool calls pinned above success rows in task-filtered view.
- Missing sanitized flag/summary: `[Raw output redacted for safety]`.
- Running attempt with progress but zero tool calls: `Tool-call ledger missing for this attempt.`

Click actions:

- Row click opens sanitized detail modal.
- `Open Task`: Task Detail Drawer.
- Filter chips call `/v1/tool-calls` with available IDs.

Rules:

- Do not fetch raw stdout/stderr.
- Do not show local log paths.
- Cap summary display to 500 characters.
- Disable copy when row is not explicitly sanitized.

## 6. Budget Panel

Default location: collapsed panel and drawer tab. Read-only in MVP.

Display fields:

- Total by currency.
- Token input/output.
- Runtime seconds.
- Cost by employee, task, attempt, provider/model, and cost type when available.
- Soft/hard limit state.
- Recent budget events.

Empty state:

- `No budget ledger events recorded.`

Abnormal state:

- Runtime/tool activity with no budget rows: `Cost missing`.
- Mixed currencies: separate rows only; no conversion.
- Soft limit exceeded: amber.
- Hard limit exceeded: red and disable retry/reassign.

Click actions:

- Budget row filters task/employee/attempt.
- Budget event opens related Task Detail Drawer.

Rules:

- No budget editing.
- No token-to-USD calculation in frontend.
- No skill pricing.
- If backend amount is missing, show `Cost missing`.

## 7. Evidence Panel

Default location: collapsed panel and drawer tab.

Display fields:

- Evidence ID and created time.
- Task ID, attempt ID, employee ID, trace ID.
- Evidence type and summary.
- Safe relative path and basename.
- Checksum status.
- Final evidence badge.
- Acceptance context: task-bound, attempt-bound, preview allowed, can accept, state/reason when backend supplies it.

Empty state:

- `No evidence submitted yet.`

Abnormal state:

- Unsafe path: red row; preview disabled.
- Missing file: yellow; preview disabled.
- Too large/truncated: show truncation state.
- Not final: neutral, insufficient for done.
- Missing task/attempt binding: `Cannot accept`.
- Done without valid final evidence: `Completion Invalid`.

Click actions:

- `Preview`: `GET /v1/evidence/{evidence_id}/safe-preview`.
- `Open Task`: Task Detail Drawer.
- `Open Trace`: only if trace API exists.

Security rules:

- Preview only through `safe-preview`.
- Render plain text/JSON/Markdown source in read-only `<pre>`.
- Do not render HTML, binary, PDF, Office, or images in MVP.
- Do not expose absolute paths, `../`, `.env`, tokens, API keys, `~/.ssh`, profile/config files, or local open/download commands.

## 8. Task Detail Drawer

Open behavior:

- Open from task card, employee current task, tool call, budget event, or evidence row.
- Fetch `GET /v1/tasks/{task_id}` as the primary payload.
- Fetch tool/budget/evidence/session filtered lists only for visible tabs.

Displayed tabs:

- Overview.
- Runtime Sessions.
- Tool Calls.
- Budget.
- Evidence.
- Timeline.

Displayed fields:

- Task metadata: title, status, priority, source, target, trace ID.
- Completion contract: valid/invalid, reason, final evidence count.
- Attempts: attempt ID, employee, adapter/runtime, status, started/finished, last heartbeat, last progress, cancellation state.
- Runtime sessions: session ID, runtime type, status, safe pid/session key, last heartbeat/progress.
- Tool calls: sanitized list grouped by attempt/session.
- Budget: summary and events.
- Evidence: final evidence, checksum, safe preview button.
- Timeline: flat event list if available.
- Controls: correction, cancel, retry, reassign, reject/reopen.

Empty states:

- `Task submitted, waiting for claim/run.`
- `No runtime session recorded yet.`
- `No tools used yet.`
- `No cost recorded yet.`
- `No evidence submitted yet.`
- `No events in trace timeline.`

Abnormal states:

- Stagnant attempt: amber row and owner action hint.
- Failed/blocked tool call: expanded sanitized error.
- Hard budget limit: sticky warning; retry/reassign disabled.
- Done without valid final evidence: red banner and reopen/reject if API exists.
- Missing timeline API: `[API Gap - Mocking Prohibited]`.

Control rules:

- No kill-session button in MVP; use task/attempt cancel.
- Request timeout is not task failure.
- Late evidence from cancelled attempts must not be shown as acceptable completion.

## Runtime Session Badges

| Backend status | UI badge | Meaning |
|---|---|---|
| `active` | green `Active` | Session can report heartbeat/progress. |
| `idle` | neutral `Idle` | Session live but not advancing a task. |
| `stale` | amber `Stale` | Freshness threshold missed. |
| `failed` | red `Failed` | Session ended with error. |
| `stopped` | gray `Stopped` | Session ended normally. |
| `cancelled` | gray `Cancelled` | Cancelled attempt cannot submit current final evidence. |

## Polling Behavior

- Poll `/v1/dashboard/cockpit` every 8 seconds.
- Pause or slow polling when `document.hidden === true`.
- Refresh immediately when the tab becomes visible.
- If API fails, keep last successful data visible and dimmed.
- Do not replace real previous data with empty panels after a request failure.

## Three-Day Delivery Plan

Day 1:

- Build Health Bar, CEO Summary, Employee Cards, Running Task Cards.
- Bind to `/v1/dashboard/cockpit` and `/v1/doctor`.
- Hide all chat/direct buttons.
- Show readiness badges and runtime/session status truthfully.

Day 2:

- Add Task Detail Drawer.
- Add Runtime Sessions and Tool Calls tabs.
- Add correction/cancel disabled/loading behavior.
- Pin failed/blocked tool calls above successful tool noise.

Day 3:

- Add read-only Budget tab/panel.
- Add Evidence panel and safe-preview modal.
- Run a real local skill-worker smoke so runtime/tool/budget/evidence data is visible.
- Run API/dashboard tests, full unit tests, doctor, and browser verification.

## File-Level Implementation Map

Frontend:

- `dashboard_templates/gemini_dashboard.html`: source template for layout, polling, cards, panels, drawer, disabled write states, and safe rendering.
- `dashboard.html`: generated runtime artifact; do not edit or commit as source.

Backend/API:

- `company_kernel/api_gateway.py`: route contracts and task control APIs.
- `company_kernel/company_dashboard.py`: cockpit aggregate payloads.
- `company_kernel/company_trace.py`: task/trace timeline fallback.
- `company_kernel/companyctl.py`: shared ledger helpers for runtime sessions, tool calls, budget, evidence, task controls.

Schema:

- `company_kernel/schema.sql`: tables for `runtime_sessions`, `agent_tool_calls`, `budget_accounts`, `budget_events`, `artifacts`, `evidence`.
- `company_kernel/schema_migrations.py`: add missing ledger fields only if API tests prove gaps.

Tests:

- `tests/test_company_kernel_core.py`: API payloads, dashboard template contract, evidence safe-preview, tool-call ledger, budget ledger, task controls, candidate/skill-worker guardrails.

## Acceptance Commands

```bash
curl -s http://127.0.0.1:8765/v1/dashboard/cockpit | python3 -m json.tool
curl -s http://127.0.0.1:8765/v1/tool-calls?limit=200 | python3 -m json.tool
curl -s http://127.0.0.1:8765/v1/runtime-sessions | python3 -m json.tool
curl -s http://127.0.0.1:8765/v1/budget-summary | python3 -m json.tool
curl -s http://127.0.0.1:8765/v1/evidence | python3 -m json.tool
python3 -m unittest discover -s tests -p 'test*.py'
bin/companyctl doctor --summary
```

Browser acceptance:

- Open `http://127.0.0.1:8780/dashboard.html`.
- Confirm data is real API data, not placeholders.
- Confirm a real task drawer shows attempt/session/tool/budget/evidence.
- Confirm skill workers do not show chat buttons.
- Confirm candidate employees do not look active.

## Antigravity Review Summary

Antigravity was used as design-review input only. Its planning replies are not execution evidence.

Round 1:

- Recommended first screen: Health, Summary, Employees, Running Tasks.
- Recommended Tool Calls, Budget, Evidence as folded panels or task drawer tabs to avoid log noise and scroll fatigue.

Round 2:

- Required field/action/API mapping for each UI region.
- Initial response used wrong `/api/v1/*` paths and SSE/WebSocket assumptions, so it was rejected for correction.

Round 3:

- Corrected all API paths to `/v1/*`.
- Confirmed REST polling only for 3-day MVP.
- Confirmed no chat buttons, no direct local file open/download, no budget mutation, no marketplace, no WorkGraph canvas, no skill pricing, no distributed rental, and no multi-tenant scope.
