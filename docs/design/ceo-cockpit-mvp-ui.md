# CEO Cockpit 3-Day MVP UI

Date: 2026-06-09

Scope: Super AI Company Kernel local-first CEO Cockpit MVP. This document is a small implementation contract for the next 3 days. It is based on `docs/research/ai-company-os-research-plan.md` and Antigravity design-review feedback.

## 1. Goal

The CEO Cockpit must let the owner see, from real Kernel data:

- Which AI employees are active, limited, candidate, skill-only, unsafe, or offline.
- What each employee is doing now.
- Which task, attempt, runtime session, and trace are involved.
- Which tools were used.
- What the task cost in tokens, runtime, and ledger currency.
- What evidence was submitted and whether it is safe/final/valid.

The UI must not treat chat, ACK, stdout, inbox files, or heartbeat as completion.

## 2. Hard MVP Boundary

Build:

- Budget Center MVP.
- Tool Call automatic-record visibility.
- Runtime Session visibility.
- Dashboard panels for employee current work, tool use, cost, and evidence.
- Task detail drawer with timeline and owner controls.

Do not build:

- Marketplace.
- WorkGraph large canvas.
- Skill pricing.
- Distributed rental.
- Payments.
- Complex multi-tenant accounts or RBAC.
- WebSocket/SSE realtime layer.
- Frontend cost estimation.
- Chat/DM product.

## 3. Primary APIs

| UI need | API |
|---|---|
| Cockpit aggregate | `GET /v1/dashboard/cockpit` |
| Doctor status | `GET /v1/doctor` |
| Employees | `GET /v1/employees` |
| Employee detail | `GET /v1/employees/{employee_id}` |
| Readiness badge | `GET /v1/agent-matrix?agents={employee_id}` |
| Task detail drawer | `GET /v1/tasks/{task_id}` |
| Trace timeline fallback | `GET /v1/traces/{trace_id}/timeline` |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=&limit=` |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&trace_id=&attempt_id=&session_id=&limit=200` |
| Budget summary | `GET /v1/budget-summary?employee_id=&task_id=&trace_id=&attempt_id=` |
| Budget events | `GET /v1/budget-events?employee_id=&task_id=&trace_id=&attempt_id=&limit=50` |
| Evidence list | `GET /v1/evidence?task_id=&employee_id=&limit=50` |
| Evidence safe preview | `GET /v1/evidence/{evidence_id}/safe-preview` |
| Correction | `POST /v1/tasks/{task_id}/correct` |
| Cancel | `POST /v1/tasks/{task_id}/cancel` |
| Retry | `POST /v1/tasks/{task_id}/retry` |
| Reassign | `POST /v1/tasks/{task_id}/reassign` |
| Reject / reopen | `POST /v1/tasks/{task_id}/reopen` |

Default actor is `owner-shift`. Control actions default to approval-request mode unless backend explicitly returns `executed: true`.

## 4. Page Layout

One high-density page:

1. Top Health Bar.
2. CEO Summary Grid.
3. Employee Cards.
4. Running Task Cards.
5. Tool Calls Panel.
6. Budget Panel.
7. Evidence Panel.
8. Task Detail Drawer.

Use existing static dashboard template, vanilla CSS, and vanilla JavaScript. Poll `/v1/dashboard/cockpit` every 8 seconds. If API fails, keep last successful data visible but dimmed and show `Offline: API Connection Lost`.

## 5. CEO Cockpit Home

Purpose: one screen answers whether the AI company is healthy and what needs owner action.

Displayed fields:

- API status, doctor ok/issue count/generated time.
- Employee counts: online, busy, active_ready, active_limited, candidate_only, online_only, task_unsupported, unsafe.
- Task counts: running, stagnant, blocked, failed, awaiting approval, done, completion_invalid.
- Runtime counts: active sessions, stale sessions, active attempts.
- Tool counts: running, success, failed, blocked.
- Budget totals by currency, token input/output, runtime seconds, soft/hard limit state.
- Evidence counts: final evidence, unsafe evidence, missing evidence, invalid completion.
- Supervisor: latest Hermes correction or pending correction ack.

Empty states:

- `No AI employees registered.`
- `No running tasks.`
- `No budget ledger events recorded.`
- `No evidence submitted yet.`
- `No recent supervisor activity.`

Abnormal states:

- API offline: red banner, stale data dimmed, do not clear populated panels.
- Doctor unhealthy: yellow banner, click opens `/v1/doctor` JSON modal.
- Hard budget limit: red banner; disable `Retry` and `Reassign`; keep `Cancel`, `Correct`, and `Reject / Reopen`.
- Completion invalid: red count and filter to invalid task cards.
- API missing field: show `API gap`, do not infer.

Click actions:

- Employee count filters Employee Cards.
- Stagnant/blocked count filters Running Task Cards.
- Budget count scrolls to Budget Panel.
- Evidence count scrolls to Evidence Panel.
- Doctor banner calls `GET /v1/doctor`.

## 6. Employee Cards

Purpose: make readiness honest. Online is not active.

Displayed fields:

- Employee ID, display name, role.
- Adapter/runtime type.
- Readiness badge: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
- Status and heartbeat freshness.
- Current task title or `Title unavailable from API`.
- Current `task_id`, `attempt_id`, `session_id`, latest progress.
- Capabilities: task/chat/tool support summary.
- Tool calls count, evidence count, cost by currency.

Empty states:

- Idle employee: `Idle. Awaiting task assignment.`
- Skill worker: `No chat; task/evidence execution only.`

Abnormal states:

- `unsafe`: red border, auto-assignment disabled.
- `candidate_only`: gray badge, `Needs structured runtime evidence before activation.`
- `online_only`: yellow badge, `Online but no task/evidence proof.`
- Heartbeat stale: amber badge.

Click actions:

- Click card: `GET /v1/employees/{employee_id}`.
- `View Task`: open Task Detail Drawer.
- `Filter Tools`: `GET /v1/tool-calls?employee_id={employee_id}&limit=200`.
- `Filter Budget`: `GET /v1/budget-summary?employee_id={employee_id}` and `GET /v1/budget-events?employee_id={employee_id}&limit=50`.
- `Filter Evidence`: `GET /v1/evidence?employee_id={employee_id}&limit=50`.
- `Verify Runtime Evidence`: `GET /v1/agent-matrix?agents={employee_id}`.

Chat rule:

- Hide chat/direct buttons for all employees in this 3-day MVP.
- Skill workers such as `ecommerce-copy-skill` still show task progress, runtime sessions, tool calls, budget, artifacts, and evidence.

## 7. Running Task Cards

Purpose: show long-running work without confusing request timeout with task failure.

Displayed fields:

- Task ID, title, priority, status.
- Assigned employee.
- Current `attempt_id`, `session_id`, `trace_id`.
- Long-task state: `running`, `progress_fresh`, `stagnant`, `correcting`, `blocked`, `failed`, `cancelled`, `done`.
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

- Stagnant: `Employee is still online, but no new progress. Continue waiting, send probe, request Hermes correction, or cancel.`
- Blocked: show blocker reason.
- Failed: show failure reason and retry/reassign controls.
- Done without final evidence: red `Completion Invalid`.
- Missing tool-call records while progress exists: `Tool-call ledger missing for this attempt`.

Click actions:

- Click card: `GET /v1/tasks/{task_id}`.
- Correct: `POST /v1/tasks/{task_id}/correct`.
- Cancel: `POST /v1/tasks/{task_id}/cancel`.
- Retry: `POST /v1/tasks/{task_id}/retry`.
- Reassign: `POST /v1/tasks/{task_id}/reassign`.
- Reject / Reopen: `POST /v1/tasks/{task_id}/reopen`.

Control body pattern:

```json
{"by":"owner-shift","attempt_id":"{attempt_id}","reason":"{owner_reason}","message":"{owner_text}","to":"{employee_id}","status":"submitted"}
```

Use only the fields required by each endpoint.

## 8. Tool Calls Panel

Purpose: answer what tools employees used.

Displayed fields:

- Tool call ID.
- Employee ID, task ID, trace ID, attempt ID, session ID.
- Tool name and type.
- Risk level and approval ID.
- Status: planned/running/success/failed/blocked/cancelled.
- Started/finished time.
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
- `Open Trace`: call `GET /v1/traces/{trace_id}/timeline`.
- Filters call `/v1/tool-calls` with `employee_id`, `task_id`, `trace_id`, `attempt_id`, or `session_id`.

Rules:

- Render latest 200 rows max.
- Do not request raw stdout/stderr.
- Do not invent `/v1/tool-calls/{tool_call_id}`.
- Cap each summary field to 500 display characters.
- Disable row copy when the row is not explicitly sanitized.

## 9. Budget Panel

Purpose: show ledger-owned cost, not frontend-estimated cost.

Displayed fields:

- Total amount grouped by currency.
- Token input/output.
- Runtime seconds.
- Cost by employee, task, attempt, and cost type.
- Soft/hard budget limits.
- Recent budget events.

Empty states:

- `No budget ledger events recorded.`

Abnormal states:

- Runtime activity with no budget rows: `Cost missing`.
- Soft limit exceeded: amber warning.
- Hard limit exceeded: red warning; disable `Retry` and `Reassign`; keep `Cancel`, `Correct`, `Reject / Reopen`.
- Mixed currencies: show per-currency rows; do not combine.
- Missing per-currency rows: `API gap: per-currency budget totals unavailable`.

Click actions:

- Employee/task row filters other panels.
- Budget event opens related task drawer.

Rules:

- No frontend token-to-USD formula.
- No exchange-rate conversion.
- No skill pricing.
- If burn rate is absent, show `Burn rate unavailable`.

## 10. Evidence Panel

Purpose: show what was actually delivered.

Displayed fields:

- Evidence ID and created time.
- Task ID, attempt ID, employee ID, trace ID.
- Evidence type and summary.
- Safe relative path and basename only.
- Checksum status.
- Final evidence badge.
- Completion contract reason when task-bound.

Empty states:

- `No evidence submitted yet.`

Abnormal states:

- Unsafe path: red row, preview disabled.
- Missing file: yellow row, preview disabled.
- Checksum mismatch: red `Checksum Error`, preview disabled.
- Not final: neutral row; not sufficient for done.
- Done task without valid final evidence: `Completion Invalid`.

Click actions:

- `Preview Content`: `GET /v1/evidence/{evidence_id}/safe-preview`.
- `Open Task`: Task Detail Drawer.
- `Open Trace`: trace timeline.

Security rules:

- Preview only through safe-preview API.
- Render only plain text, JSON text, or Markdown source in read-only `<pre>`.
- Preview container hard limit is 100 KB in the browser. If backend returns more text, truncate client display and show `Preview truncated for MVP safe viewer`.
- Do not render image/PDF/Office/HTML/binary in MVP.
- Do not create `file://` links.
- Do not expose absolute `/Users/...` paths.
- Do not use `/v1/evidence/{evidence_id}/content` for new UI.

## 11. Task Detail Drawer

Purpose: one task lifecycle view.

Open behavior:

- Click task card or task link.
- Fetch exactly one primary payload: `GET /v1/tasks/{task_id}`.
- Only call `GET /v1/traces/{trace_id}/timeline` if task payload lacks timeline.

Displayed sections:

- Task metadata: title, status, priority, source, target, trace ID.
- Completion contract: valid, reason, final evidence count.
- Attempts: attempt ID, employee, adapter/runtime, status, started/finished, last heartbeat, last progress, cancellation state.
- Runtime sessions: session ID, runtime type, status, pid/session key if safe, last heartbeat/progress.
- Tool calls: sanitized list grouped by attempt/session.
- Budget: summary and events.
- Evidence: final evidence, checksum, preview button.
- Timeline: vertical text event list.
- Controls: correction, cancel, retry, reassign, reject/reopen.
- Control buttons enter disabled/loading state until the API returns, so repeated owner clicks cannot create duplicate approval requests.

Empty states:

- `Task submitted, waiting for claim/run.`
- `No runtime session recorded yet.`
- `No tools used yet.`
- `No cost recorded yet.`
- `No evidence submitted yet.`
- `No events in trace timeline.`

Abnormal states:

- Stagnant attempt: amber attempt row and owner action hint.
- Failed/blocked tool call: expanded sanitized error.
- Hard budget limit: sticky warning; disable retry/reassign.
- Done without valid final evidence: red banner plus `Reject / Reopen`.
- Timeline API returns nested graph instead of flat list: `Timeline API gap: flat event list unavailable`.
- Timeline fallback may expose a debug-only `Copy raw JSON` button, but it must not attempt to render a graph/tree/canvas.

## 12. Runtime Session Badges

| Backend status | UI badge | Meaning |
|---|---|---|
| `active` | green `Active` | Session is live and can report heartbeat/progress. |
| `idle` | neutral `Idle` | Session is live but not advancing a task. |
| `stale` | amber `Stale` | Backend freshness threshold missed. |
| `failed` | red `Failed` | Session ended with error. |
| `stopped` | gray `Stopped` | Session ended normally. |
| `cancelled` | gray `Cancelled` | Cancelled attempt cannot submit current final evidence. |

No global Runtime Session page in the 3-day MVP. Show sessions on employee cards, task cards, and task drawer only.

## 13. False-State Guardrails

- Employee evidence history is trusted only from `/v1/evidence?employee_id={employee_id}` or task drawer payload.
- Current task title missing means `Title unavailable from API`, not guessed from messages.
- Backend state owns `running`, `stagnant`, `failed`, and `stale`; browser time can show age but cannot declare failure.
- Unsanitized tool-call fields are redacted.
- Mixed-currency budget is displayed per currency only.
- `done` requires backend-valid final evidence for the same task/attempt context.
- Skill workers hide chat but keep execution monitoring.
- Request timeout keeps stale data dimmed; it does not replace a populated panel with empty state.
- Evidence checksum mismatch disables preview and acceptance.

## 14. Three-Day Delivery Plan

Day 1:

- Cockpit layout: health bar, summary grid, employee cards, task cards.
- 8-second REST polling from `/v1/dashboard/cockpit`.
- Pause polling while `document.hidden === true`; refresh immediately when the tab becomes visible again.
- Doctor banner via `/v1/doctor`.
- Candidate/skill-only/unsafe employee badges.

Day 2:

- Task Detail Drawer from `GET /v1/tasks/{task_id}`.
- Runtime session visibility on employee/task/drawer.
- Tool Calls panel and sanitized row detail.
- Owner controls for correct/cancel/retry/reassign/reopen.

Day 3:

- Budget panel from budget summary/events.
- Evidence panel and safe preview modal.
- Local real-data smoke: a skill worker task must produce visible runtime/tool/budget/evidence state.
- Full unit tests and doctor verification.

## 15. Acceptance Commands

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

## 16. Antigravity Review Log

Antigravity was used only as a design-review employee. Its replies are critique input, not execution evidence.

Round 1 summary:

- Required areas are Top Health Bar, CEO Summary, Employee Cards, Running Task Cards, Tool Calls, Budget, Evidence, and Task Detail Drawer.
- Most important missing states are API offline with stale data preserved, hard budget limit policy, done-without-final-evidence invalid marker, unsanitized tool-call redaction, unsafe evidence path/checksum handling, and API gap labels.
- All owner actions must go through task-bound Kernel APIs with `owner-shift`.
- No-go boundaries are no chat/DM, no marketplace, no WorkGraph canvas, no skill pricing, no frontend cost calculation, no graph libraries, no WebSocket/SSE, and no kill-session button.

Round 2 requirement:

- Antigravity confirmed this remains a 3-day MVP and not a marketplace, WorkGraph canvas, skill pricing, distributed rental, or multi-tenant expansion.
- It requested four small UI safeguards: disable control buttons while requests are pending, cap safe-preview rendering in the browser, pause polling for hidden tabs, and add debug-only raw JSON copy for unusable timeline payloads.
