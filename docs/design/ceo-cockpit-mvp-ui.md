# CEO Cockpit 3-Day MVP UI

Date: 2026-06-09

Scope: local-first Super AI Company Kernel CEO Cockpit UI contract. This design is based on `docs/research/ai-company-os-research-plan.md` and two Antigravity design-review rounds. It is a 3-day MVP only.

## Goal

The CEO Cockpit must let the owner see, from real Kernel data:

- what every AI employee is doing now;
- which task, attempt, runtime session, and trace it belongs to;
- which tools were used;
- how much budget was spent in tokens, runtime, and ledger currency;
- what evidence was submitted and whether it is final, safe, and task-bound.

The UI must not treat chat, ACK, stdout, inbox files, or heartbeat as completion.

## Non-Goals

Do not build or expose:

- marketplace;
- WorkGraph large canvas;
- skill pricing;
- distributed rental;
- payments;
- complex multi-tenant accounts or RBAC;
- chat/DM product;
- WebSocket/SSE realtime layer.

All employee chat/direct buttons are hidden in this MVP, including for active employees, candidates, and skill workers. Skill workers such as `ecommerce-copy-skill` show task execution, runtime sessions, tool calls, budget, artifacts, and evidence, but no chat affordance.

## API Contract

Existing or planned APIs must be labeled honestly. If an API is not implemented, mark it `[API Gap - Mocking Prohibited]`; the frontend may show empty state or disabled controls only. It must not fabricate tasks, evidence, sessions, token counts, or costs.

| UI need | API | Status |
|---|---|---|
| Cockpit aggregate | `GET /v1/dashboard/cockpit` | required |
| Doctor status | `GET /v1/doctor` | required |
| Employees | `GET /v1/employees` | required |
| Employee detail | `GET /v1/employees/{employee_id}` | required |
| Running task list | `GET /v1/dashboard/cockpit` `task_cards` / `long_tasks`; fallback `GET /v1/tasks?limit=50` client-filtered to running/stagnant/blocked | required |
| Task detail drawer | `GET /v1/tasks/{task_id}` | required |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=&limit=` | required |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&trace_id=&attempt_id=&session_id=&limit=200` | required |
| Budget summary | `GET /v1/budget-summary?employee_id=&task_id=&trace_id=&attempt_id=` | required |
| Budget events | `GET /v1/budget-events?employee_id=&task_id=&trace_id=&attempt_id=&limit=50` | required |
| Evidence list | `GET /v1/evidence?task_id=&employee_id=&limit=50` | required |
| Evidence safe preview | `GET /v1/evidence/{evidence_id}/safe-preview` | required |
| Trace timeline fallback | `GET /v1/traces/{trace_id}/timeline` | `[API Gap - Mocking Prohibited]` if absent |
| Correction | `POST /v1/tasks/{task_id}/correct` | required or disabled |
| Cancel | `POST /v1/tasks/{task_id}/cancel` | required or disabled |
| Retry | `POST /v1/tasks/{task_id}/retry` | visible disabled if absent |
| Reassign | `POST /v1/tasks/{task_id}/reassign` | visible disabled if absent |
| Reject / reopen | `POST /v1/tasks/{task_id}/reopen` | visible disabled if absent |

Default actor is `owner`. Write actions must show disabled/loading state while pending so repeated owner clicks cannot create duplicate approval requests.

## Page Layout

One high-density page:

1. Top Health Bar.
2. CEO Summary Grid.
3. Employee Cards.
4. Running Task Cards.
5. Tool Calls Panel.
6. Budget Panel.
7. Evidence Panel.
8. Task Detail Drawer.

Use existing static dashboard template, vanilla CSS, and vanilla JavaScript. Poll `/v1/dashboard/cockpit` every 8 seconds. Pause polling when `document.hidden === true`; refresh immediately when the tab becomes visible. If API fails, keep last successful data visible, dim it, and show `Offline: API Connection Lost`; do not replace populated panels with empty state.

## 1. Top Health Bar

Displayed fields:

- API status.
- Doctor status and issue count.
- Last successful sync timestamp.
- Polling interval and manual `Refresh Now`.
- Current filter chips, if any.

Empty state:

- `Waiting for Kernel API.`

Abnormal states:

- API offline: red banner, last good data dimmed.
- Doctor unhealthy: yellow banner with issue count.
- Missing required API: `API Gap - Mocking Prohibited`.

Click actions:

- `Refresh Now`: re-fetch cockpit payload and dependent visible panels.
- Doctor banner: `GET /v1/doctor` and show JSON detail modal.

## 2. CEO Summary Grid

Displayed fields:

- Employee counts: online, busy, active_ready, active_limited, candidate_only, online_only, task_unsupported, unsafe.
- Task counts: running, stagnant, blocked, failed, awaiting approval, done, completion_invalid.
- Runtime counts: active sessions, stale sessions, active attempts.
- Tool counts: running, success, failed, blocked.
- Budget totals by currency, token input/output, runtime seconds, soft/hard limit state.
- Evidence counts: final evidence, unsafe evidence, missing evidence, invalid completion.
- Supervisor status: latest correction or pending correction ack when backend supplies it; otherwise `No recent supervisor activity`.

Empty states:

- `No AI employees registered.`
- `No running tasks.`
- `No budget ledger events recorded.`
- `No evidence submitted yet.`

Abnormal states:

- Hard budget limit: red card; disable retry and reassign controls globally.
- Completion invalid: red card and filter action.
- API missing field: `API gap`, not inferred.

Click actions:

- Employee count filters Employee Cards.
- Stagnant/blocked count filters Running Task Cards.
- Budget card scrolls to Budget Panel.
- Evidence card scrolls to Evidence Panel.

## 3. Employee Cards

Displayed fields:

- Employee ID, display name, role.
- Adapter/runtime type.
- Readiness badge: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
- Status and heartbeat freshness.
- Current task title or `Title unavailable from API`.
- Current `task_id`, `attempt_id`, `session_id`, latest progress.
- Capabilities: task/chat/tool support summary.
- Runtime session count.
- Tool call count and failed tool count.
- Evidence count and final evidence count.
- Cost by currency and token input/output when available.

Empty states:

- Idle active employee: `Idle. Awaiting task assignment.`
- Candidate: `Awaiting activation. Needs structured runtime evidence.`
- Skill worker: `No chat; task/evidence execution only.`

Abnormal states:

- `unsafe`: red border, auto-assignment disabled.
- `candidate_only`: gray dashed border, not counted as active capacity.
- `online_only`: yellow badge, `Online but no task/evidence proof.`
- Heartbeat stale/offline: dim card.

Click actions:

- Click card: `GET /v1/employees/{employee_id}`.
- `View Task`: open Task Detail Drawer.
- `Filter Tools`: `GET /v1/tool-calls?employee_id={employee_id}&limit=200`.
- `Filter Budget`: budget summary/events filtered by `employee_id`.
- `Filter Evidence`: `GET /v1/evidence?employee_id={employee_id}&limit=50`.

Rules:

- Hide all chat/direct/message buttons.
- Do not promote candidate or online-only employees visually into active staff.

## 4. Running Task Cards

Displayed fields:

- Task ID, title, priority, status.
- Assigned employee.
- Current `attempt_id`, `session_id`, `trace_id`.
- Long-task state: `running`, `progress_fresh`, `stagnant`, `correcting`, `blocked`, `failed`, `cancelled`, `done`.
- Elapsed duration.
- Latest progress message and timestamp.
- Heartbeat/progress freshness.
- Completion contract: valid/invalid and reason.
- Tool calls count and failed tool calls count.
- Cost summary by currency.
- Evidence count and final evidence status.

Empty states:

- `No running tasks.`
- `Awaiting employee claim.`

Abnormal states:

- Stagnant: `Employee is still online, but no new progress. Continue waiting, send probe, request correction, or cancel.`
- Claimed but no runtime session: `Cold start: waiting for runtime session.`
- Blocked: show blocker reason.
- Failed: show sanitized failure reason.
- Done without final evidence: red `Completion Invalid`.
- Progress exists but tool calls are missing: `Tool-call ledger missing for this attempt`.

Click actions:

- Click card: `GET /v1/tasks/{task_id}`.
- Correct: `POST /v1/tasks/{task_id}/correct`.
- Cancel: `POST /v1/tasks/{task_id}/cancel`.
- Retry/Reassign/Reject/Reopen: visible but disabled with `Backend API pending (API Gap)` if backend does not support the endpoint.
- Any write action immediately disables its button, shows `Submitting...`, and keeps that disabled/loading state until the API responds or the request times out. Duplicate owner clicks must not create duplicate correction, cancel, retry, reassign, or approval requests.

## 5. Tool Calls Panel

Displayed fields:

- Tool call ID.
- Employee ID, task ID, trace ID, attempt ID, session ID.
- Tool name and tool type.
- Risk level and approval ID.
- Status: planned, running, success, failed, blocked, cancelled.
- Started/finished time and duration.
- Sanitized input summary, output summary, error summary.

Empty states:

- `No tool calls recorded yet.`
- Filtered view: `No tool calls under this filter.`

Abnormal states:

- Failed: red row with sanitized error.
- Blocked by policy: yellow row with policy code.
- Missing `sanitized === true`: show `[Raw output redacted for safety]` and disable copy.
- Running attempt with progress but zero tool calls: `Tool-call ledger missing for this attempt`.

Click actions:

- Row click opens sanitized detail modal from the hydrated row.
- `Open Task`: open Task Detail Drawer.
- `Open Trace`: call trace timeline only if available.
- Filters call `/v1/tool-calls` with `employee_id`, `task_id`, `trace_id`, `attempt_id`, or `session_id`.

Rules:

- Render latest 200 rows max.
- Do not request raw stdout/stderr.
- Do not invent `/v1/tool-calls/{tool_call_id}`.
- Cap each summary field to 500 display characters.
- Disable copy when the row is not explicitly sanitized.

## 6. Budget Panel

Displayed fields:

- Total amount grouped by currency.
- Token input/output.
- Runtime seconds.
- Cost by employee, task, attempt, provider/model, and cost type when available.
- Soft/hard budget limits and utilization.
- Recent budget events.

Empty state:

- `No budget ledger events recorded.`

Abnormal states:

- Runtime activity with no budget rows: `Cost missing`.
- Soft limit exceeded: amber warning.
- Hard limit exceeded: red warning; disable retry and reassign.
- Mixed currencies: show per-currency rows; do not combine.
- Missing per-currency rows: `API gap: per-currency budget totals unavailable`.
- Burn-rate data absent: `Burn rate unavailable`.

Click actions:

- Employee/task/attempt rows filter other panels.
- Budget event opens related task drawer.

Rules:

- No frontend token-to-USD formula.
- No exchange-rate conversion.
- No skill pricing.

## 7. Evidence Panel

Displayed fields:

- Evidence ID and created time.
- Task ID, attempt ID, employee ID, trace ID.
- Evidence type and summary.
- Safe relative path and basename only.
- Checksum status.
- Final evidence badge.
- Completion contract reason when task-bound.

Empty state:

- `No evidence submitted yet.`

Abnormal states:

- Unsafe path: red row, preview disabled.
- Missing file: yellow row, preview disabled.
- Checksum mismatch: red `Checksum Error`, preview disabled.
- Not final: neutral row; not sufficient for done.
- Done task without valid final evidence: `Completion Invalid`.
- Too large to preview: disabled preview, `Too large to preview (>100KB)`.

Click actions:

- `Preview Content`: `GET /v1/evidence/{evidence_id}/safe-preview`.
- `Open Task`: Task Detail Drawer.
- `Open Trace`: trace timeline only if available.

Security rules:

- Preview only through safe-preview API.
- Render only plain text, JSON text, or Markdown source in read-only `<pre>`.
- Browser preview hard limit is 100 KB; truncate display if needed.
- Do not render image/PDF/Office/HTML/binary in MVP.
- Do not create `file://` links.
- Do not expose absolute `/Users/...` paths.
- Do not use `/v1/evidence/{evidence_id}/content` for new UI.

## 8. Task Detail Drawer

Open behavior:

- Click task card or task link.
- Fetch exactly one primary payload: `GET /v1/tasks/{task_id}`.
- Only call trace timeline fallback if task payload lacks timeline and the API exists.

Displayed tabs:

- Overview.
- Runtime.
- Tool Calls.
- Budget.
- Evidence.
- Timeline.

Displayed fields:

- Task metadata: title, status, priority, source, target, trace ID.
- Completion contract: valid, reason, final evidence count.
- Attempts: attempt ID, employee, adapter/runtime, status, started/finished, last heartbeat, last progress, cancellation state.
- Runtime sessions: session ID, runtime type, status, safe pid/session key, last heartbeat/progress.
- Tool calls: sanitized list grouped by attempt/session.
- Budget: summary and events.
- Evidence: final evidence, checksum, preview button.
- Timeline: vertical text event list.
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
- Hard budget limit: sticky warning; disable retry/reassign.
- Done without valid final evidence: red banner plus `Reject / Reopen` if API exists.
- Timeline API returns nested graph instead of flat list: `Timeline API gap: flat event list unavailable`.

Control rules:

- Disable and show loading state during POST.
- The clicked write button must enter disabled/loading state before the network request is sent, and must remain disabled until success, error, or timeout is rendered. The drawer must not allow repeated owner clicks to enqueue duplicate control requests.
- Correction message max display contract is 1,000 characters; backend may enforce stricter validation.
- Retry/Reassign/Reopen may be shown disabled when backend endpoints are missing.
- No kill-session button in MVP; use task/attempt cancel.

## Runtime Session Badges

| Backend status | UI badge | Meaning |
|---|---|---|
| `active` | green `Active` | Session is live and can report heartbeat/progress. |
| `idle` | neutral `Idle` | Session is live but not advancing a task. |
| `stale` | amber `Stale` | Backend freshness threshold missed. |
| `failed` | red `Failed` | Session ended with error. |
| `stopped` | gray `Stopped` | Session ended normally. |
| `cancelled` | gray `Cancelled` | Cancelled attempt cannot submit current final evidence. |

No global Runtime Session page in the 3-day MVP. Show sessions on employee cards, task cards, and task drawer only.

## False-State Guardrails

- Employee readiness comes from backend data; online is not active.
- Candidate and online-only employees are visually degraded and excluded from active capacity.
- Skill workers hide chat but keep execution monitoring.
- Current task title missing means `Title unavailable from API`, not guessed from messages.
- Backend state owns `running`, `stagnant`, `failed`, and `stale`; browser time can show age but cannot declare failure.
- Unsanitized tool-call fields are redacted.
- Mixed-currency budget is displayed per currency only.
- `done` requires backend-valid final evidence for the same task/attempt context.
- Request timeout does not equal task failure.
- Request timeout keeps stale data visible and dimmed; it does not create empty panels.
- Evidence checksum mismatch disables preview and acceptance.
- `[API Gap - Mocking Prohibited]` means disabled or empty state only, never mock data.

## Three-Day Delivery Plan

Day 1:

- Build Cockpit layout: health bar, summary grid, employee cards, running task cards.
- Poll `/v1/dashboard/cockpit` every 8 seconds with hidden-tab pause.
- Add doctor banner through `/v1/doctor`.
- Render honest readiness badges: candidate, online-only, skill-only, unsafe.
- Hide all chat/direct buttons.

Day 2:

- Add task detail drawer from `GET /v1/tasks/{task_id}`.
- Show runtime sessions on cards and drawer.
- Add Tool Calls panel and sanitized row detail.
- Add correction/cancel controls where backend supports them; show retry/reassign/reopen disabled if API gap.

Day 3:

- Add Budget Panel from budget summary/events.
- Add Evidence Panel and safe-preview modal.
- Run a local skill-worker smoke so runtime/tool/budget/evidence data appears from real ledger state.
- Run focused dashboard/API tests, full unit tests, and doctor.

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
- Confirm the page shows real API data, not placeholders.
- Confirm a real task drawer shows attempt/session/tool/budget/evidence.
- Confirm skill workers do not show chat buttons.
- Confirm candidate employees do not look active.

## Antigravity Review Log

Antigravity was used only as a design-review employee. Its replies are critique input, not execution evidence.

Round 1:

- Cut anything that makes the 3-day MVP look like marketplace, WorkGraph, skill pricing, distributed rental, or multi-tenant admin.
- Add stale-data preservation, hard budget policy, completion-invalid markers, unsanitized tool redaction, unsafe evidence path/checksum handling, API-gap labels, loading/disabled states, safe-preview cap, hidden-tab polling pause, and skill-worker chat removal.

Round 2:

- Keep `Retry`, `Reassign`, and `Reject / Reopen` visible in the task drawer as owner mental-model controls, but disable them with `Backend API pending (API Gap)` until backend support exists.
- Runtime Session visibility comes before Tool Calls; Tool Calls before Budget; Budget before Evidence preview.
- All unimplemented APIs must be labeled `[API Gap - Mocking Prohibited]`.
- Hide all Chat/Direct entry points in this MVP so the cockpit remains task, tool, budget, and evidence driven.
- Clarify that Running Task Cards are sourced from `/v1/dashboard/cockpit` `task_cards` / `long_tasks`, with `/v1/tasks?limit=50` only as a fallback list source.
- Explicitly require write-action loading/disabled states to prevent duplicate owner correction/cancel/retry/reassign/approval requests.
