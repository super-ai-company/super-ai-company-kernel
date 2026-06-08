# CEO Cockpit MVP UI Design Specification

**Date**: 2026-06-09
**Scope**: 3-Day MVP UI for Local CEO Operations
**Target File**: `docs/design/ceo-cockpit-mvp-ui.md`

---

## 1. Overview & Core Goal

The CEO Cockpit is a high-density, read-heavy operations dashboard designed to answer the core question:
> **"Which AI employee is doing what, which tools did it use, what did it cost, and what evidence did it submit?"**

This is an operational visibility console using real backend ledger records and system states.

This document is based on `docs/research/ai-company-os-research-plan.md`, current Company Kernel code/API inspection, and two Antigravity design-review rounds. It is a 3-day MVP UI contract only, not a marketplace or distributed rental product spec.

### Key Design Changes (Round 2 Review):
1. **Ledger-Driven Budgeting**: All cost values (`budget_events.amount`) are owned and written by the backend ledger; the frontend never computes or aggregates prices itself. Currency visualization supports multiple currencies; if mixed currencies are present, display `"mixed"`.
2. **Sanitized Tool Calls**: Tool calls are hydrated by the backend with security/privacy fields (`sanitized=true`, `raw_available=false`, `redaction_policy`). The list endpoint returns at most 200 items and supports filtering parameters (`employee`, `task`, `trace`, `attempt`, `session`).
3. **Backend-Driven Runtime Sessions**: The dashboard relies on `generated_at` anchor returned by the `/v1/dashboard/cockpit` API. Session status calculation is determined solely by the backend. **Kill Session is deferred/out of scope** for the 3-day MVP; only **Cancel Task** and **Cancel Attempt** are allowed.
4. **Aggregation-First Loading**: The home view prioritizes a single aggregation API `/v1/dashboard/cockpit`. Detailed operational panels and drawers use lazy loading via target endpoints.
5. **Safe Evidence Previews**: Evidence content preview is restricted to the safe content API. Absolute paths and unsafe file types are blocked.

### Explicit Non-Goals

Do not build or design these in this MVP:

1. Marketplace, public employee store, public node onboarding, or distributed computer renting.
2. WorkGraph large canvas, DAG editor, SVG topology map, or workflow designer.
3. Skill pricing, Stripe, wallet, invoices, settlement, or revenue share.
4. Complex multi-tenant account model, organization permissions, or cloud user system.
5. Freeform global chat playground.
6. WebSocket-first realtime, interactive terminal, or raw live log stream.
7. Mock production dashboard data.

---

## 2. Global Architecture & API Strategy

To prevent UI lag and unnecessary polling overhead:
1. **Initial Load / Poll**: Fetch `/v1/dashboard/cockpit` as the single primary endpoint for the home dashboard status and aggregates.
2. **Lazy Loading**: When selecting an employee or task, lazy-load detailed records (`GET /v1/employees/{employee_id}`, `GET /v1/tasks/{task_id}`) rather than pre-fetching or filtering global datasets client-side.
3. **State Integrity**: Frontend is a stateless visualizer. The backend defines attempt states (`stagnant`, `blocked`, `failed`, `cancelled`, `done`) and session health.

### API Mapping Matrix

| UI area / Component | API Endpoint | Parameters / Filters | Hydration / Formatting Rules |
|---|---|---|---|
| **Cockpit Summary** | `GET /v1/dashboard/cockpit` | None | Returns counts, active session details, and global budget rollups. |
| **Employee List** | `GET /v1/employees` | None | Shows all registered AI employees. |
| **Employee Detail** | `GET /v1/employees/{employee_id}` | None | Lazy-loaded on card click. Contains history, tool calls, budget. |
| **Readiness Badge** | `GET /v1/agent-matrix` | `agents={employee_id}` | Determines active/limited/unsafe badges based on agent-matrix. |
| **Runtime Sessions** | `/v1/dashboard/cockpit` (aggregation) | None | Anchored with `generated_at`. Status determined by backend. |
| **Tool Calls List** | `GET /v1/tool-calls` | `employee_id`, `task_id`, `trace_id`, `attempt_id`, `session_id` | **Max 200 items**. Must use backend-hydrated `sanitized=true` logs. |
| **Budget Summary** | `GET /v1/budget-summary` | `employee_id`, `task_id` | Returns `currency`, `currencies`, `limit_status`, `budget_limits`. |
| **Budget Events** | `GET /v1/budget-events` | None | Ledger records. Costs written by backend ledger. |
| **Evidence Preview** | `GET /v1/evidence/{evidence_id}/content` | None | Restricted to safe content API preview only. |
| **Task Details** | `GET /v1/tasks/{task_id}` | None | Lazy-loaded. Contains attempts, sessions, budget, completion contract. |
| **Task Control** | `POST /v1/tasks/{task_id}/cancel` <br> `POST /v1/tasks/{task_id}/correct` | None | Cancel / correct tasks. Cancel attempt is supported. |

---

## 3. UI Zone Specifications

The single dashboard page consists of **7 visible zones** and **1 right-side drawer**.

### Zone 1: CEO Cockpit Home Summary
*   **Purpose**: One-screen status summary of the AI company operation.
*   **Displayed Fields**:
    *   Employee Totals: Total, online, active_ready, active_limited, candidate_only, online_only, task_unsupported, unsafe.
    *   Task Totals: Running, stagnant, blocked, failed, awaiting approval, done.
    *   Runtime Totals: Active sessions, stale sessions, and `generated_at` timestamp.
    *   Tool Totals: Running, success, failed, blocked.
    *   Budget Totals: Total estimated cost, currency (or `"mixed"` if mixed currencies are returned in `budget_summary.currencies`), token input, token output, runtime seconds.
    *   Evidence Totals: Total submitted, issue counts.
    *   Supervisor Signal: Latest Hermes correction notice, pending correction ack status.
*   **Empty State**:
    *   If no data is present: `"No active AI employees or tasks yet. Register an employee or submit a task."`
    *   Show setup shortcut actions: `[Create Employee]`, `[Submit Task]`, `[Run Local Smoke Test]`.
    *   If budget ledger is empty: `"No budget records yet."`
*   **Abnormal State**:
    *   API Offline: Red top-bar banner `"Offline: API Connection Lost"`. Previous data remains visible but dimmed.
    *   Doctor Unhealthy: Yellow top-bar banner `"System Diagnostics Unhealthy: X issues found"` (link to `/bin/companyctl doctor`).
    *   Budget Hard Limit Exceeded: Red sticky alert `"Hard Limit Exceeded: Paid executions suspended"` (fetched from `limit_status`). Disable new task creation buttons.
    *   Completion Invalid: Task count where tasks are done but lack valid final evidence.
*   **Click Actions**:
    *   Click Employee Counts: Jumps to & filters Employee Cards.
    *   Click Stagnant/Blocked Tasks: Focuses and filters Running Tasks.
    *   Click Budget Total: Scrolls and focuses Budget Panel.
*   **API**: `GET /v1/dashboard/cockpit` (Primary aggregator).

---

### Zone 2: Employee Cards
*   **Purpose**: Display readiness and active execution context for each AI employee.
*   **Displayed Fields**:
    *   Employee ID, Name, Role, Status.
    *   Readiness Badge: `active_ready`, `active_limited`, `candidate_only`, `online_only`, `task_unsupported`, `no_reply`, `unsafe`.
    *   Heartbeat: Last seen timestamp and freshness.
    *   Current Work: Task ID, current `attempt_id`, active `session_id`.
    *   Financials & Deliverables: Total accumulated cost, evidence count.
*   **Empty State**:
    *   No employees: `"No AI employees registered."`
    *   Idle employee: `"Idle. Awaiting task assignment."`
    *   Skill-only employee (no chat capability): `"No chat; task/evidence execution only."`
*   **Abnormal State**:
    *   `unsafe` status: Red border around the card, disables auto-task assignment.
    *   `candidate_only` status: Grey card with message `"Needs structured runtime evidence before activation."`
    *   Stale Heartbeat (no updates for > 5 minutes): Yellow warning icon, orange border.
*   **Click Actions**:
    *   Click Card: Opens the Employee Detail drawer (triggering `GET /v1/employees/{employee_id}`).
    *   Filter Buttons: Quick filter global Tool Calls, Budget, or Evidence panels for this employee.
    *   *Constraint*: The `"Send Direct Message"` action is disabled if the employee readiness is `task_unsupported` or runtime type is `skill`.
*   **API**: `GET /v1/employees` (List), `GET /v1/employees/{employee_id}` (Detail rollup), `GET /v1/agent-matrix`.

---

### Zone 3: Running Task Cards
*   **Purpose**: Real-time monitoring of tasks and execution status.
*   **Displayed Fields**:
    *   Task ID, Title, Priority, Status.
    *   Assigned Employee, current Attempt ID, Trace ID.
    *   Status/State: Running, stagnant, correcting, blocked, failed, done.
    *   Heartbeat Freshness: Last active attempt timestamp.
    *   Latest Progress Message: Plain-text updates from the backend agent.
    *   Completion validation contract indicator.
*   **Empty State**:
    *   No running tasks: `"No running tasks."`
    *   Unassigned: `"Awaiting employee claim."`
*   **Abnormal State**:
    *   `stagnant` (No progress for N minutes): Orange indicator card.
    *   `blocked`: Red border with error/blocker reason.
    *   Invalid completion: Red indicator `"Completion Invalid: Done state set without valid final evidence"` if `completion_contract.valid` is false.
*   **Click Actions**:
    *   Click Card: Opens the Task Detail drawer.
    *   Control Actions:
        *   `[Send Correction]` (`POST /v1/tasks/{task_id}/correct`)
        *   `[Cancel Attempt]` (Only allow canceling task/attempt. **No Kill Session**).
        *   `[Retry / Reassign]`
*   **API**: `GET /v1/dashboard/cockpit`, `GET /v1/tasks/{task_id}`.

---

### Zone 4: Tool Calls Panel
*   **Purpose**: Low-level operational audit log showing tool execution details.
*   **Displayed Fields**:
    *   Tool Call ID, Timestamp.
    *   Associated Employee ID, Task ID, Attempt ID, Session ID.
    *   Tool Name & Type (e.g., `shell`, `browser`, `file`, `model`).
    *   Execution Status (running, success, failed, blocked).
    *   Sanitized Input/Output summary (based on backend `sanitized=true`, `raw_available=false`, `redaction_policy` values).
*   **Empty State**:
    *   `"No tool calls recorded yet."`
*   **Abnormal State**:
    *   Failed Tool Call: Highlighted in red with error output summary.
    *   Blocked by Policy: Highlighted in yellow showing the policy violation code.
    *   *Security fallback*: If `sanitized` flag is missing or false, redact all raw output on the client side, showing only: `[Raw output redacted for safety]`.
*   **Click Actions**:
    *   Filter buttons: Filter list by Employee, Task, Attempt, or Session (supports `employee_id`, `task_id`, `trace_id`, `attempt_id`, `session_id` query parameters).
    *   Row Click: Opens detail modal showing sanitized JSON structure.
*   **API**: `GET /v1/tool-calls` (Max 200 items in list).

---

### Zone 5: Budget Panel
*   **Purpose**: Estimated internal resource usage tracking (ledger-based).
*   **Displayed Fields**:
    *   Total Cost & Currency (displays `"mixed"` if multiple currencies exist in `budget_summary.currencies`).
    *   Soft / Hard limits and Limit Status (`limit_status` / `budget_limits` from backend).
    *   Visual breakdown: Cost by employee, Cost by task, Cost by type (`model_api`, `local_compute`, etc.).
    *   Recent Budget Events list.
*   **Empty State**:
    *   `"No budget ledger events recorded."`
*   **Abnormal State**:
    *   Over Soft Limit: Orange border warning.
    *   Over Hard Limit: Red banner, locks dashboard run-actions.
    *   Cost Mismatch: Task shows runtime activity but $0.00 cost (flagged as warning).
*   **Click Actions**:
    *   Click Employee/Task: Filters other panels by selection.
*   **API**: `GET /v1/budget-summary`, `GET /v1/budget-events`. All cost calculations/aggregates must read from these endpoints; frontend never calculates values itself.

---

### Zone 6: Evidence Panel
*   **Purpose**: Immutable deliverables ledger.
*   **Displayed Fields**:
    *   Evidence ID, Created Time.
    *   Task ID, Attempt ID, Employee ID.
    *   Deliverable Type (`file`, `screenshot`, `text`, `link`).
    *   Safe relative path (e.g., `evidence/reports/delivery.md`).
    *   Checksum (SHA-256).
    *   Delivery summary.
*   **Empty State**:
    *   `"No evidence submitted yet."`
*   **Abnormal State**:
    *   Unsafe Path: Highlighted in red. The preview action is completely disabled.
    *   Missing File: Yellow text warning `"File not found on disk"`.
*   **Click Actions**:
    *   `[Preview Content]`: Safe Content Preview via safe API only.
*   **API**: `GET /v1/evidence` (List), `GET /v1/evidence/{evidence_id}/content` (Safe Content preview).

---

### Zone 7: Task Detail Drawer (Right-Side Drawer)
*   **Purpose**: The single source of truth for an individual task's life cycle.
*   **Sections / Fields**:
    *   Task Metadata: Title, status, employees involved, trace ID.
    *   Completion Contract Status: Displays whether completion is valid.
    *   Attempts: Attempt ID, employee, adapter/runtime, status, started/finished time, heartbeat/progress freshness.
    *   Runtime Sessions: Session ID, runtime type, status, PID if available, last heartbeat/progress.
    *   Tool Calls: Tool name/type/status/risk, sanitized input/output summaries, approval linkage.
    *   Budget: Total cost, currency, tokens, runtime seconds, budget limit warnings, recent events.
    *   Evidence: Final evidence, checksum, safe preview link, completion contract reason.
    *   Execution Timeline: Ascending chronological list of events (task created -> employee claimed -> attempt started -> tool calls -> budget events -> evidence submitted).
    *   Control Panel: `[Send Correction]`, `[Cancel Attempt]` (**Kill Session is disabled**).
*   **Empty State**:
    *   No attempts: `"Task submitted, waiting for claim/run."`
    *   No tool calls: `"No tools used yet."`
    *   No budget: `"No cost recorded yet."`
    *   No evidence: `"No evidence submitted yet."`
*   **Abnormal State**:
    *   Stale or stagnant attempt: highlight status row and show latest progress timestamp.
    *   Failed or blocked tool call: expand sanitized error summary only.
    *   Budget over hard limit: sticky red warning and block new paid run actions.
    *   Done without valid final evidence: red `completion invalid` banner; legacy `tasks.evidence_path` alone is not enough.
*   **Lazy Loading**: Hydrated via `GET /v1/tasks/{task_id}` only when opened.
*   **Click Actions**:
    *   `[Send Correction]`: `POST /v1/tasks/{task_id}/correct`.
    *   `[Cancel Attempt]`: `POST /v1/tasks/{task_id}/cancel`.
    *   `[Retry]`: `POST /v1/tasks/{task_id}/retry`.
    *   `[Reassign]`: `POST /v1/tasks/{task_id}/reassign`.
    *   `[Open Evidence Preview]`: `GET /v1/evidence/{evidence_id}/content`.
    *   `[Copy Trace ID]`: copies trace ID only, not local file paths.
*   **API**: `GET /v1/tasks/{task_id}`, `GET /v1/traces/{trace_id}/timeline`, `GET /v1/tool-calls?task_id={task_id}`, `GET /v1/runtime-sessions?task_id={task_id}`, `GET /v1/budget-summary?task_id={task_id}`, `GET /v1/evidence?task_id={task_id}`.

---

## 4. Anti-Deviation & Safety Rules (Guardrails)

To prevent implementation scope creep and maintain security:
1.  **Price Aggregation Ban**: The frontend **MUST NOT** calculate total budgets or perform any currency conversions. It only displays fields returned by the `budget_summary` API. If `currencies` contains multiple types, the UI displays `"mixed"`.
2.  **No Direct Kill Session**: Removing execution runtimes is limited to `Cancel Task` and `Cancel Attempt` via standard task endpoints. Do not write or call `/v1/runtime-sessions/{id}/kill` or similar.
3.  **Maximum Item Constraints**: The Tool Calls panel list is capped at **200 items**. If more records exist, the UI relies on filter parameters (`employee_id`, `task_id`, `session_id`, etc.) to narrow the scope.
4.  **Evidence Sandboxing**: All evidence preview files must go through the safe preview endpoint: `GET /v1/evidence/{evidence_id}/content`. The frontend must never attempt to read files using local raw paths (e.g., `file:///...`) or render raw HTML/JS content inline without strict text escaping.
5.  **Sanitized Log Output Only**: Tool call inputs and outputs must only render sanitized text fields. If a record indicates `sanitized: false`, the frontend must apply a default local fallback block and refuse to display the unredacted content.
6.  **No WebSocket Real-Time Logic**: UI synchronization must use a standard 5-to-10-second interval REST poll. Do not configure WebSockets, Socket.io, or complex SSE fallback scripts.
7.  **No Mock Runtime Truth**: If the API has no runtime session, tool call, budget event, or evidence record, the UI must show an explicit empty state. It must not infer work completion from heartbeat, chat text, stdout, ACK, or inbox files.
8.  **Skill Worker Chat Suppression**: `task_unsupported` or skill-only employees must hide chat/direct buttons but still show task execution, progress, tool calls, budget, artifacts, and evidence.

---

## 5. 3-Day Build & Acceptance Verification Plan

### Day 1: Aggregation & High-Density UI Layout
*   Implement layout zones using CSS Grid/Flexbox (dense, clean layout).
*   Integrate `GET /v1/dashboard/cockpit` to populate all numbers and active totals on Cockpit Home.
*   Setup REST polling interval (default: 8 seconds).

### Day 2: Filterable Logs & Lazy-Loaded Drawers
*   Implement the right-side Task Detail Drawer, loading data lazily via `GET /v1/tasks/{task_id}`.
*   Implement Tool Calls list (max 200 items limit) with employee/task/session filtering.
*   Wire up Cancel Task/Attempt actions (No Kill Session button).

### Day 3: Security Guardrails & Local Verification
*   Implement Safe Content API evidence preview modal.
*   Apply the "mixed" currency logic for mixed budget currency cases.
*   Run local tests and verify compliance via CLI doctor.

---

## 6. Acceptance Commands

```bash
# Run backend validation test suites
python3 -m unittest discover -s tests -p 'test*.py'

# Run system doctor diagnostics
bin/companyctl doctor --summary

# Query core Aggregator API
curl -s http://127.0.0.1:8780/v1/dashboard/cockpit | python3 -m json.tool

# Query filterable Tool Calls list (confirm max 200 limits and parameters)
curl -s http://127.0.0.1:8780/v1/tool-calls?limit=200 | python3 -m json.tool

# Query runtime, budget, and evidence ledgers used by the MVP panels
curl -s http://127.0.0.1:8780/v1/runtime-sessions | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/budget-summary | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/evidence | python3 -m json.tool
```

Browser verification:

1. Open `http://127.0.0.1:8780/dashboard.html`.
2. Confirm the home screen uses real counts from `GET /v1/dashboard/cockpit`.
3. Click an employee card and confirm readiness, session, current task, latest tool call, cost, and evidence appear from `GET /v1/employees/{employee_id}` or lazy ledger APIs.
4. Click a task and confirm the drawer shows attempt, runtime session, tool calls, budget, evidence, completion contract, and trace timeline.
5. Confirm `candidate_only` employees are not shown as `active_ready`.
6. Confirm skill-only or `task_unsupported` workers do not show chat/direct buttons but still show task/evidence monitoring.

## 7. Antigravity Multi-Round Review Record

Round 1 questions raised by Antigravity:

1. Budget must be backend-ledger calculated, not frontend price math.
2. Mixed currencies must display `mixed` and avoid unsafe client aggregation.
3. Hard budget limits need backend policy support; UI disabling alone is not security.
4. Tool calls must be sanitized before display; raw payloads stay hidden.
5. Tool call lists must support filtering and a hard list cap.
6. Runtime stale/stagnant state must use backend/server time, not browser clock.
7. Kill Session is too risky for the MVP and must be deferred.
8. Home page should avoid N+1 polling by using `/v1/dashboard/cockpit`.

Round 2 answers verified by Codex and approved by Antigravity:

1. `budget_events.amount` is written by the backend ledger; UI displays `budget_summary`.
2. `budget_summary` returns `currency`, `currencies`, `limit_status`, and `budget_limits`; mixed currencies display `mixed`.
3. `agent_tool_calls` hydration returns `sanitized=true`, `raw_available=false`, and `redaction_policy`.
4. `GET /v1/tool-calls` supports `employee_id`, `task_id`, `trace_id`, `attempt_id`, and `session_id` filters, with a 200-record maximum.
5. `/v1/dashboard/cockpit` includes `generated_at`; backend owns session/stagnant state.
6. The MVP exposes `Cancel Task/Attempt`, not `Kill Session`.
7. Evidence preview stays on `GET /v1/evidence/{evidence_id}/content`.
8. Antigravity approved the 3-day MVP scope after these constraints were added.
