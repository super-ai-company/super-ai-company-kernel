# CEO Cockpit MVP UI

Date: 2026-06-09

Scope: 3-day MVP UI only. This document is based on `docs/research/ai-company-os-research-plan.md`, current Company Kernel APIs, and an Antigravity design-review pass. It does not include Marketplace, WorkGraph canvas, Skill pricing, distributed renting, complex tenancy, payment, or multi-tenant billing.

## MVP Goal

The CEO Cockpit must answer this in one screen:

> Which AI employee is doing what, which tools did it use, what did it cost, and what evidence did it submit?

This is an AI employee operations cockpit, not a chat playground. The UI must use real Company Kernel API/database records. No mock data, no "online means active", no ACK-as-done, and no evidence-free completion.

## 3-Day MVP Implementation Lock

Build only the operational visibility layer needed for a local CEO dashboard:

1. Budget Center MVP: show estimated task/employee/project cost from the internal ledger only.
2. Tool Call automatic recording: show command/API/browser/file/model actions as ledger records, not chat claims.
3. Runtime Session visualization: show live/stale/stopped execution sessions separately from employee identity.
4. Employee work visibility: show what each employee is doing, which task/attempt/session it belongs to, which tools it used, what it cost, and what evidence it submitted.

Do not include Marketplace, WorkGraph large canvas, Skill pricing, distributed renting, multi-tenant billing, payment, public node onboarding, or complex tenant/account management.

## Current API Contract

Use these current or near-term APIs. If a field is missing, backend must add it before the UI claims support.

| UI area | API | Current mapping |
|---|---|---|
| Cockpit summary | `GET /v1/dashboard/cockpit` | Available; already returns counts, long tasks, runtime/tool/budget rollups. |
| Employees | `GET /v1/employees`, `GET /v1/employees/{employee_id}` | Available; detail should include work history, sessions, tool calls, budget, evidence. |
| Readiness matrix | `GET /v1/agent-matrix` | Available; use for readiness badge, not just heartbeat. |
| Runtime sessions | `GET /v1/runtime-sessions?employee_id=&task_id=&trace_id=` | Available; add UI filters by employee/task. |
| Tool calls | `GET /v1/tool-calls?employee_id=&task_id=&attempt_id=&session_id=` | Available; session filtering is required for runtime-session drilldown. |
| Budget | `GET /v1/budget-summary`, `GET /v1/budget-events` | Available; add soft/hard limit rollup from budget accounts. |
| Evidence | `GET /v1/evidence`, `GET /v1/evidence/{evidence_id}/content` | Available; preview must be safe and path-whitelisted. |
| Task detail | `GET /v1/tasks/{task_id}` | Available; must show attempts, progress, sessions, tools, budget, evidence, and completion contract. |
| Task control | `POST /v1/tasks/{task_id}/correct/cancel/retry/reassign` | Available; high-risk actions should route through approval if configured. |
| Approvals | `GET /v1/approvals`, approve/deny endpoints | Available; keep inside cockpit/audit, not separate MVP product. |

## Layout

Use a single dashboard page with 7 visible zones:

1. CEO Cockpit home summary.
2. Employee cards.
3. Running task cards.
4. Tool Calls panel.
5. Budget panel.
6. Evidence panel.
7. Task detail drawer.

Keep the layout as cards, tables, and a right-side drawer. Do not build graph canvas, marketplace pages, skill shop, global free-chat, or WebSocket-first realtime. MVP refresh is REST polling every 5-10 seconds; SSE can remain a later enhancement.

## 1. CEO Cockpit Home

Purpose: one-screen owner view of the AI company.

Displayed fields:

- Employee totals: total, online, active_ready, active_limited, candidate_only, online_only, task_unsupported, unsafe.
- Task totals: running, stagnant, blocked, failed, awaiting approval, done.
- Runtime totals: active sessions, stale sessions, latest session heartbeat.
- Tool totals: running, success, failed, blocked, recent total.
- Budget totals: estimated amount, currency, token input, token output, runtime seconds.
- Evidence totals: final evidence count, recent evidence count, evidence issues.
- Supervisor signal: latest Hermes correction, stagnant detection, pending correction ack.

Empty state:

- Show: `No active AI employees or tasks yet. Register an employee or submit a task.`
- Show setup actions only: `Create employee`, `Submit task`, `Run local smoke`.
- For missing ledgers show explicit empty copy: `No runtime/tool/budget records yet.` Never show fake numbers.

Abnormal state:

- API offline: red read-only banner; keep last rendered static data dimmed.
- Doctor unhealthy: yellow banner with exact issue count and link to diagnostics.
- Budget hard limit exceeded: red banner; disable new task run action until approval.
- Done task without final evidence: red `completion invalid` count.

Click actions:

- Click employee count: jump to Employee Cards filtered by abnormal/readiness state.
- Click stagnant tasks: filter Running Task Cards to `stagnant`.
- Click tool calls: scroll/open Tool Calls panel.
- Click estimated cost: scroll/open Budget panel.
- Click evidence: scroll/open Evidence panel.

API:

- Primary: `GET /v1/dashboard/cockpit`.
- Secondary: `GET /v1/runtime-sessions`, `GET /v1/tool-calls`, `GET /v1/budget-summary`, `GET /v1/evidence`.

## 2. Employee Cards

Purpose: show whether an AI employee can really work, not just whether it is online.

Displayed fields:

- `employee_id`, `name`, `runtime`, `role`, `status`.
- Readiness badge: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
- Heartbeat freshness and `last_seen_at`.
- Current task id/title/status and current `attempt_id`.
- Active runtime `session_id` and session status.
- Latest tool call name/type/status.
- Estimated cost for this employee: amount, tokens, runtime seconds.
- Evidence count and latest final evidence summary.

Empty state:

- No employees: `No AI employees registered.`
- Idle employee: `Idle, awaiting task assignment.`
- Skill worker without chat: `No chat; task/evidence only.`

Abnormal state:

- `candidate_only`: grey badge; `Needs structured runtime evidence before activation.`
- `online_only`: yellow badge; `Online but no task/evidence proof.`
- `task_unsupported`: neutral badge; hide chat/direct button, keep task execution/evidence monitoring.
- `unsafe`: red badge; disable automatic assignment.
- Stale heartbeat: orange border and `Heartbeat stale`.
- Session active but no progress: orange `progress stagnant` chip.

Click actions:

- Click card: open Employee Detail drawer via `GET /v1/employees/{employee_id}`.
- `View tasks`: filter tasks by employee.
- `View tool calls`: filter `/v1/tool-calls?employee_id={employee_id}`.
- `View sessions`: filter `/v1/runtime-sessions?employee_id={employee_id}`.
- `View evidence`: filter `/v1/evidence?employee_id={employee_id}` if supported; otherwise filter client-side from evidence list.
- Hide `Send message` when runtime is `skill` or readiness is `task_unsupported`.

API:

- `GET /v1/employees`.
- `GET /v1/employees/{employee_id}`.
- `GET /v1/agent-matrix?agents={employee_id}`.
- `GET /v1/runtime-sessions?employee_id={employee_id}`.
- `GET /v1/tool-calls?employee_id={employee_id}`.
- `GET /v1/budget-summary?employee_id={employee_id}`.

Backend rollup needed:

- Employee detail must return `work_history`, `runtime_sessions`, `tool_calls`, `budget_summary`, `budget_events`, and `evidence_records` so the card/detail does not infer from unrelated global data.
- Employee card may show a compact subset, but the detail drawer must be backed by `GET /v1/employees/{employee_id}` rather than client-side guessing from global lists.

## 3. Running Task Cards

Purpose: show active work and owner intervention options.

Displayed fields:

- `task_id`, title, priority, status.
- assigned/claimed employee.
- `trace_id`, latest `attempt_id`.
- Long-task state: running, stagnant, correcting, blocked, failed, cancelled, done.
- heartbeat freshness and progress freshness.
- latest progress message and timestamp.
- latest tool call.
- current estimated cost.
- final evidence status.

Empty state:

- `No running tasks.`
- Submitted but unclaimed: `Awaiting employee claim.`

Abnormal state:

- `stagnant`: orange bar; `Employee still online, but no new progress for N minutes.`
- `blocked`: red bar with blocker reason.
- `failed`: red bar with failed attempt/error.
- `cancelled`: grey disabled state; old attempt cannot submit done.
- Done-like state without final evidence: red `completion invalid`.
- `completion_contract.valid=false`: block accept/done actions and show the exact reason from `GET /v1/tasks/{task_id}`.

Click actions:

- Click card: open Task Detail drawer.
- `Send Correction`: `POST /v1/tasks/{task_id}/correct`.
- `Cancel`: `POST /v1/tasks/{task_id}/cancel`.
- `Retry`: `POST /v1/tasks/{task_id}/retry`.
- `Reassign`: `POST /v1/tasks/{task_id}/reassign`.
- `View trace`: open timeline section from task detail.

API:

- `GET /v1/dashboard/cockpit`.
- `GET /v1/tasks/{task_id}`.
- `GET /v1/tasks/{task_id}/attempts` if split endpoint is retained.
- `GET /v1/traces/{trace_id}/timeline`.

## 4. Tool Calls Panel

Purpose: show what employees actually did at command/API/browser/file/tool level.

Displayed fields:

- `tool_call_id`.
- employee id.
- task id, attempt id, trace id.
- session id.
- tool name and type: shell, browser, file, api, openclaw, telegram, local_script, model, other.
- status: running, success, failed, blocked, cancelled.
- risk level.
- input summary.
- output summary.
- started/finished time.
- `sanitized: true/false` once backend supports it.

Empty state:

- `No tool calls yet. Tool calls appear when employees run commands, use APIs, read/write files, or operate adapters.`

Abnormal state:

- `failed`: red row with sanitized error summary.
- `blocked`: yellow row with policy/approval reason.
- long-running tool call: orange `long running tool call`.
- unsanitized raw output: hide by default and show `raw output hidden`.

Click actions:

- Open tool call detail modal.
- Filter by task, attempt, employee, or session.
- Jump to task drawer.
- Jump to trace timeline.

API:

- `GET /v1/tool-calls`.
- `GET /v1/tool-calls?task_id={task_id}`.
- `GET /v1/tool-calls?attempt_id={attempt_id}`.
- `GET /v1/tool-calls?employee_id={employee_id}`.
- `GET /v1/tool-calls?session_id={session_id}`.

Security:

- Never show raw stdout/stderr by default.
- Use sanitized summaries only.
- Hide tokens, `.env`, `~/.ssh`, `api key`, absolute private paths.
- If backend returns no `sanitized` flag, the frontend still treats raw output as unsafe and renders only summaries.

## 5. Budget Panel

Purpose: show what the AI company spent, as an estimated internal ledger.

Displayed fields:

- total amount and currency.
- token input/output.
- runtime seconds.
- cost by employee.
- cost by task.
- cost by cost type: model_api, local_compute, external_api, human_review, other.
- recent budget events.
- soft limit and hard limit status when `budget_accounts` is configured.

Empty state:

- `No budget events yet. Costs appear when adapters record model/tool/runtime usage.`

Abnormal state:

- over soft limit: orange warning.
- over hard limit: red `budget locked` state.
- missing currency or mixed currency: `mixed currency` warning.
- task has model/tool activity but no budget event: yellow `cost missing` marker.
- soft/hard limit exceeded: show `limit_status` from `GET /v1/budget-summary` and require owner approval before starting more paid work.

Click actions:

- Click employee cost: filter employee card and tool calls.
- Click task cost: open task drawer.
- Click event: open timeline section.

API:

- `GET /v1/budget-summary`.
- `GET /v1/budget-summary?task_id={task_id}`.
- `GET /v1/budget-summary?employee_id={employee_id}`.
- `GET /v1/budget-events`.

MVP rule:

- Budget is an estimated SQLite ledger, not real payment.
- No Stripe, wallet, rental billing, marketplace settlement, skill pricing, or multi-tenant invoices.

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

- `No evidence submitted yet.`

Abnormal state:

- unsafe path: red row, no preview link.
- final task without evidence: red `completion invalid` marker.
- superseded/rejected artifact: grey and not included downstream by default.
- evidence path missing on disk: yellow `file missing` marker.

Click actions:

- Safe preview: `GET /v1/evidence/{evidence_id}/content`.
- Open task drawer.
- Open trace timeline.

API:

- `GET /v1/evidence`.
- `GET /v1/evidence?task_id={task_id}`.
- `GET /v1/evidence/{evidence_id}/content`.

Security:

- Only show files from workspace/artifacts/evidence/final/reports allowlisted roots.
- Do not expose absolute paths.
- Block `../`, `.env`, token, api key, config/profile secrets, `~/.ssh`.

## 7. Task Detail Drawer

Purpose: one task's complete operational truth.

Displayed sections:

- Header: task title, priority, status, target employee, claimed employee, trace id.
- Status contract: timeout vs heartbeat stale vs progress stagnant vs blocked vs failed vs done.
- Attempts: attempt id, employee, adapter, status, started/finished, heartbeat/progress times.
- Runtime sessions: session id, runtime type, status, heartbeat.
- Tool calls: tool name/type/status/risk and sanitized summaries.
- Budget: total cost, tokens, runtime seconds, event list, budget limit warnings.
- Timeline: task/event/attempt/session/tool/budget/artifact/handoff/evidence in time order.
- Evidence: final evidence and safe preview link.
- Completion contract: `valid`, `reason`, `final_evidence_count`, `safe_final_evidence_count`, and summary.
- Approvals: pending or decided approvals.

Empty state:

- no attempts: `Task submitted, waiting for claim/run.`
- no tool calls: `No tools used yet.`
- no budget: `No cost recorded yet.`
- no evidence: `No evidence submitted yet.`

Abnormal state:

- stale attempt: highlight stale point in timeline.
- failed tool call: expand sanitized error summary.
- budget exceeded: sticky warning.
- pending approval: sticky yellow action bar.
- cancelled attempt: grey state; disable `accept done`.
- done without final evidence: red `completion invalid` banner.
- legacy `tasks.evidence_path` without final evidence table record: red `missing_final_evidence`; old path alone is not accepted as done.

Click actions:

- `Send Correction`.
- `Cancel Attempt`.
- `Retry`.
- `Reassign`.
- `Approve/Deny`.
- `Open Evidence Preview`.
- `Copy Trace ID`.

API:

- `GET /v1/tasks/{task_id}`.
- `GET /v1/tasks/{task_id}/attempts` if used separately.
- `GET /v1/traces/{trace_id}/timeline`.
- `GET /v1/tool-calls?task_id={task_id}`.
- `GET /v1/runtime-sessions?task_id={task_id}`.
- `GET /v1/budget-summary?task_id={task_id}`.
- `GET /v1/evidence?task_id={task_id}`.

## 3-Day Build Plan

Day 1: make real data visible.

- Cockpit home cards for employees, tasks, runtime sessions, tool calls, budget, evidence.
- Employee cards show readiness, current task, session, latest tool call, cost, evidence count.
- REST polling every 5-10 seconds; no WebSocket.
- Empty states must be explicit, not fake data.

Day 2: make task truth inspectable.

- Task detail drawer shows attempts, runtime sessions, tool calls, budget, evidence, approvals, and timeline.
- Tool Calls panel and Budget panel become filterable by employee/task/attempt/session.
- Evidence preview uses safe content API only.
- Skill workers hide chat/direct action but keep task/progress/evidence monitor.

Day 3: make it verifiable on this Mac.

- Run one local task that produces runtime session, tool call, budget event, artifact/evidence.
- Verify `candidate_only` employees are not shown as active_ready.
- Verify `task_unsupported` skill worker can show task progress/evidence without chat.
- Verify dashboard has no empty fake panels when DB has real rows.
- Fix correctness, state labels, and visibility only; no visual polish expansion.

## Backend Gaps To Close Before UI Can Be Honest

- Employee card rollup: current task, active session, latest tool call, cost, evidence count.
- Budget limit rollup: compare `budget_summary` against `budget_accounts.soft_limit/hard_limit`.
- Tool call filter: support `session_id` on `/v1/tool-calls`.
- Sanitized marker: return `sanitized: true` for tool/timeline summaries that hide raw output or secrets.
- Completion guard: task drawer must flag done-like task without final evidence.

Current implementation status:

- Employee detail rollup: implemented in `GET /v1/employees/{employee_id}`.
- Budget limit rollup: implemented in `GET /v1/budget-summary` as `limit_status`.
- Tool call `session_id` filter: implemented in `GET /v1/tool-calls`.
- Completion contract: implemented in `GET /v1/tasks/{task_id}` as `completion_contract`.
- Sanitized flag: still target-state; frontend must stay conservative until every backend producer marks records.

## Acceptance Commands

```bash
python3 -m unittest discover -s tests -p 'test*.py'
bin/companyctl doctor --summary
curl -s http://127.0.0.1:8780/v1/dashboard/cockpit | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/runtime-sessions | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/tool-calls | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/budget-summary | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/evidence | python3 -m json.tool
```

Browser verification:

- Open `http://127.0.0.1:8780/dashboard.html`.
- Confirm real counts appear in Cockpit home.
- Click an employee card and confirm work history, runtime sessions, tool calls, budget, evidence appear.
- Click a running/done task and confirm the drawer shows attempt, progress, tool calls, budget, evidence.
- Confirm no chat button appears for `task_unsupported` skill workers.

## Anti-Scope Rules

Do not build:

- Marketplace.
- WorkGraph canvas.
- Skill pricing page.
- Distributed renting.
- Multi-tenant billing.
- Payment integration.
- Freeform global chat.
- WebSocket-first realtime.
- Mock dashboard data.

## Antigravity Review Notes

Round 1 reviewed the MVP as frontend/product reviewer. Required corrections:

- Keep CEO Cockpit, employee readiness cards, running task cards, Tool Calls, Budget, Evidence, and Task Detail drawer.
- Add employee rollup fields so cards can show active session, latest tool call, current cost, and evidence count.
- Add budget soft/hard limit status instead of only total estimated cost.
- Add `/v1/tool-calls?session_id=...` filtering.
- Add sanitized markers for hidden raw logs/secrets.
- Avoid WorkGraph DAG/SVG/canvas, global chat playground, real payment, and WebSocket-first realtime.
- Use 10-second REST polling as the MVP refresh model.

Round 2 passed the revised design as a focused 3-day MVP. Deferrals:

- Full approval workflow can be delayed behind direct `cancel/retry/reassign/correct` controls in MVP, as long as dangerous real external sends remain approval-gated elsewhere.
- Supervisor Signal can be basic/read-only first; core employee/task/tool/budget/evidence rendering has priority.
- If backend does not yet return `sanitized: true`, frontend may still hide raw output and apply conservative client-side redaction, but backend sanitization remains the target.

Round 3 reviewed the implementation lock. Required constraints:

- Keep the MVP centered on Budget Center, Tool Calls, Runtime Sessions, and Evidence-backed employee work visibility.
- Treat `GET /v1/employees/{employee_id}` as the required rollup source for employee detail.
- Treat `GET /v1/tool-calls?session_id=...` as required for session drilldown.
- Treat `completion_contract.valid=false` as a UI blocker for accepting done-like tasks.
- Keep Marketplace, WorkGraph canvas, Skill pricing, distributed renting, and multi-tenant billing out of this 3-day scope.

Round 4 passed the final 3-day MVP scope with these implementation risks:

- Backend `sanitized: true` is not guaranteed on every producer yet, so frontend must keep raw output hidden and render conservative summaries.
- REST polling 5-10 seconds is acceptable for MVP, but the page should avoid independently hammering all panels when one cockpit payload can supply rollups.
- Evidence preview must keep using the safe content API and must never render unsafe paths or executable HTML/JS as trusted content.
- Seven visible zones plus one task drawer can become visually noisy; MVP should prioritize dense operational cards/tables over extra decoration.

Round 4 explicit non-goals:

- No WorkGraph canvas, DAG, or SVG topology.
- No Marketplace, Skill pricing, store, Stripe, wallet, invoices, or rental billing.
- No distributed node renting or complex tenant/account management.
- No WebSocket-first realtime or global free-chat playground.
- No mock data in production dashboard views.
