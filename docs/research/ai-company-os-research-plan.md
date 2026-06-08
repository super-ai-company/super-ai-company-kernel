# AI Company OS Research Plan

Date: 2026-06-09

Scope: Super AI Company Kernel / WorkGraph OS 2.0 research plan. This document is research and planning only; it does not change runtime code, local OpenClaw services, employees, or business workflows.

## Executive Conclusion

There is no single mature open-source product that fully matches our target: an AI employee hiring and operations kernel where agents can be registered, supervised, budgeted, corrected, audited, and asked to deliver evidence through a durable company ledger.

The market already has strong pieces:

- Paperclip / Multica: closest product inspiration for local agent discovery, employee-like UX, and multi-agent workspace experience.
- Langfuse / VoltAgent / Idun Platform: strongest references for observability, traces, evaluation, governance, and operational dashboards.
- LangGraph / CrewAI / AutoGen Studio: orchestration, multi-agent workflow, and conversation-to-task design references.
- Dify / Flowise: workflow builder and productized LLM app UI references.
- OpenHands: coding-agent runtime, workspace execution, and developer-agent UX reference.
- kagent: Kubernetes-native autonomous agent and infrastructure operations reference.

Our differentiator should not be another agent chat UI. The product should become the control plane for AI employees:

- Who is online, busy, candidate, active-limited, or unsafe.
- What each employee is doing now.
- What task/attempt/trace they are working on.
- What they have already done.
- What they spent in tokens, money, time, compute, and approvals.
- What files/artifacts/evidence they produced.
- What is blocked, stagnant, failed, cancelled, or waiting for owner approval.
- What can be retried, reassigned, corrected, or accepted.

Bottom layer first is correct. Without a durable task ledger, execution attempts, evidence layer, budget center, approval governance, and audit log, the product will look good but fail during long-running real work.

## Current Kernel Baseline

The repository already has a solid first-phase kernel direction:

- Employee registry and runtimes.
- Tasks, task metadata, relations, projects, and acceptances.
- Messages, conversations, external messages, direct communication, and links.
- Heartbeats, locks, approvals, audit logs, company events.
- Adapter runs and execution attempts.
- Task workspaces, artifacts, evidence, handoffs, and task context packages.
- API gateway endpoints for employees, tasks, messages, conversations, approvals, heartbeats, attendance, adapter runs, projects, locks, followups, runtimes, skills, events, SSE, evidence, artifacts, handoffs, failures, traces, workspace pruning, dashboard cockpit, and watchdog.
- Dashboard work has started, but the product needs a stronger CEO operations model.

Key missing 2.0 primitives:

- First-class token/cost/compute accounting.
- First-class tool-call ledger.
- Runtime session model separate from employee identity.
- Risk policy and approval routing model.
- Goal/KPI/WorkGraph model beyond current tasks/projects.
- Replay/timeline API built for CEO and supervisor views.
- Stronger evidence validation and acceptance workflow.

## Competitor Matrix

| Product | Positioning | Open Source / Local Deploy | Agent Management | Task / Goal System | Cost / Token Stats | Approval / Governance | Logs / Trace / Audit | Evidence / Files | Dashboard | What We Should Borrow | What Not To Copy |
|---|---|---:|---|---|---|---|---|---|---|---|---|
| Paperclip | Local AI agent workspace / agent selection UX reference | Needs source verification before adoption | Strong UX inspiration for discovering local agents | Likely session/task oriented | Unknown | Unknown | Unknown | Unknown | Strong onboarding feel from user-shared UI direction | Simple agent connection, friendly hiring-like flow | Do not treat agent connection as proof of employee readiness |
| Multica | Multi-agent company/workspace manager reference | Needs official source verification before adoption | Strong employee roster, squads, task-board concept | Strong product inspiration | Unknown | Unknown | Some visible task flow likely | Unknown | Strong local agent picker and workplace UX | Employee model, squads, task dispatch, skill sharing | Do not copy if backend ledger/evidence/governance is weak |
| Langfuse | LLM observability, traces, prompt management, evals | Yes, self-hosting available | Not an employee manager | Trace-oriented, not company task ledger | Strong LLM usage/cost reference | Some governance/eval workflows | Strong tracing and observability | Not business evidence layer | Strong technical observability UI | Trace model, spans, cost accounting, eval concepts | Do not make CEO read low-level LLM spans as the main UI |
| VoltAgent | TypeScript AI agent framework with observability and console | Yes | Agent framework and console | Workflow/agent oriented | Observability reference | Limited compared with company governance | Strong dev observability direction | Not core evidence registry | Useful agent console | Developer experience and agent observability | Do not couple kernel to one runtime/framework |
| OpenHands | Autonomous coding agent runtime | Yes, local/self-host options | Coding agent runtime, not company registry | Issue/task execution | Not main focus | Runtime permissions/sandbox ideas | Execution logs | Workspace files and code diffs | Good developer-agent UX | Workspace execution, coding-agent isolation | Do not let one coding runtime own company state |
| AutoGen Studio | UI for Microsoft AutoGen multi-agent workflows | Yes | Agent/team configuration | Conversation and workflow oriented | Limited | Limited | Session logs | Not evidence-first | Useful prototyping UI | Agent teams and conversation design | Avoid black-box endless agent chat |
| CrewAI | Multi-agent crews, tasks, tools, flows | Yes | Strong role/crew model | Strong task and crew abstraction | Limited in OSS core | Limited | Logs/traces possible through integrations | Not durable evidence registry | Commercial UI exists | Role-based crews and task delegation | Do not rely on prompt roles instead of ledger state |
| LangGraph | Durable agent/workflow graph framework | Yes | Runtime-level nodes/agents | Strong graph/state machine | Through LangSmith/Langfuse integrations | Human-in-the-loop patterns | Strong graph execution trace via ecosystem | Not company evidence by default | Graph visualization references | Durable execution, checkpoints, state graph | Do not outsource company domain model to framework internals |
| Dify | LLM app platform and workflow builder | Yes | App/workflow oriented, not employee OS | Good workflow/task-ish builder | Useful app usage analytics | App governance, credentials, permissions | Logs and app traces | File inputs/outputs but not employee evidence ledger | Productized dashboard | App builder polish and workflow UX | Do not become a generic chatbot/app builder |
| Flowise | Visual low-code LLM orchestration | Yes | Node/chain focused | Workflow graph builder | Limited | Limited | Debug logs | Not evidence-first | Good low-code graph UI | Visual workflow editor ideas | Do not reduce work to prompt nodes without durable tasks |
| kagent | Kubernetes-native AI agent automation | Yes | Infra agents in Kubernetes context | Autonomous infra operations | Infra cost not core | Kubernetes policy/security reference | Operational logs | Infra action evidence possible | Infra control plane | K8s-native agent governance | Not relevant for local desktop-first MVP except future cluster mode |
| Idun Platform | AI agent observability, evaluation, risk/governance platform | Commercial / verify deployment options | Agent monitoring, not employee execution | Evaluation and oversight | Cost/usage governance reference | Strong risk/eval positioning | Strong monitoring/eval direction | Not company artifact handoff by default | Enterprise governance inspiration | Risk scoring, evaluation, safety dashboards | Do not make governance only post-hoc monitoring |

## Detailed Notes

### Paperclip

Paperclip is useful as a product reference because its apparent direction overlaps with the user-facing need: local agent discovery, selecting an agent runtime, and making the first-run experience feel approachable. This matters because our current kernel is technically strong but still too backend-shaped.

Borrow:

- Local agent discovery and connection UX.
- Clear distinction between installed agent, online agent, selected agent, and ready-to-work employee.
- Onboarding that feels like hiring an employee, not configuring a server.

Do not copy:

- Do not treat "agent found online" as employee activation.
- Do not let UI state replace kernel evidence.
- Do not build a session-only product without attempts, trace, cost, and evidence.

### Multica

Multica is the closest strategic reference from the user's product direction: AI employees, squads, task dispatch, agent communication, and skill sharing. It appears closer to an AI company workplace than generic LLM workflow tools.

Borrow:

- Employee roster and company-like mental model.
- Squad/team grouping.
- Task board and visible progress.
- Skill sharing as product language.
- "Connect local agents, then assign work" UX.

Do not copy:

- Do not make the platform only a coordination surface.
- Do not accept chat replies as completion.
- Do not hide backend state machine, attempts, blockers, evidence, approvals, and cost.

Strategic difference: Multica can inspire the experience, but Super AI Company Kernel should own the durable control plane. If Multica is "agent workplace", our target is "agent ERP plus operations cockpit".

### Langfuse

Langfuse is one of the strongest references for LLM observability: traces, sessions, generations, prompts, evaluations, and usage/cost monitoring. It answers many engineering-level visibility questions, but not the whole employee company problem.

Borrow:

- Trace/span hierarchy.
- Cost and token accounting model.
- Prompt/version tracking ideas.
- Evaluation datasets and scoring loops.
- Clean observability UI patterns.

Do not copy:

- Do not make trace spans the main CEO experience.
- Do not focus only on LLM calls; our unit of work is task/attempt/evidence.
- Do not ignore non-LLM actions: browser operation, file edits, local scripts, OpenClaw bridge, Telegram delivery, and approvals.

### VoltAgent

VoltAgent is useful as a modern TypeScript agent framework and developer console reference. It is especially relevant for agent observability, runtime ergonomics, and developer-friendly instrumentation.

Borrow:

- Agent runtime console patterns.
- Framework-agnostic instrumentation style.
- Developer-friendly logs and status display.
- Separation between agent code and observability console.

Do not copy:

- Do not bind the kernel to TypeScript or any one runtime.
- Do not make framework-level "agent" equal company-level "employee".

### OpenHands

OpenHands is a strong reference for autonomous coding agents that can operate in a workspace, execute commands, edit files, and produce development results.

Borrow:

- Workspace execution UX.
- Coding-agent sandbox and permission ideas.
- Task transcript plus file changes.
- Human intervention model for risky operations.

Do not copy:

- Do not give any single coding agent full company authority.
- Do not let file changes become evidence without registration and validation.
- Do not conflate code task completion with business task completion.

### AutoGen Studio

AutoGen Studio is a reference for configuring multi-agent teams and observing conversations/workflows.

Borrow:

- Agent/team composition UX.
- Conversation-to-workflow prototyping.
- Visual inspection of multi-agent interaction.

Do not copy:

- Do not allow endless black-box agent conversations.
- Every important conversation must map to task, approval, blocker, RFC, artifact, or evidence.

### CrewAI

CrewAI provides a strong role/task/crew abstraction and is useful for thinking about employee roles.

Borrow:

- Role-based agents.
- Crew/task delegation language.
- Process definitions for sequential or hierarchical work.

Do not copy:

- Prompt-defined roles are not enough. The kernel needs permissions, heartbeats, attempts, evidence, and audit.
- Do not rely on "manager agent says done" as final acceptance.

### LangGraph

LangGraph is the strongest reference for durable graph execution and stateful agent workflows. It is very relevant to WorkGraph OS, but it should be treated as an adapter/runtime option, not the source of truth for company state.

Borrow:

- Durable graph execution.
- Checkpointing.
- Human-in-the-loop patterns.
- Conditional edges and recovery patterns.
- Graph visualization ideas.

Do not copy:

- Do not let graph framework internals define employee identity, budget, approval, or evidence.
- The company ledger must remain independent.

### Dify

Dify is a polished LLM app platform with workflow building, prompt/app management, and deployment experience.

Borrow:

- Productized workflow UI.
- Dataset/tool/model management UX.
- App logs and deployment settings.
- Non-engineer-friendly configuration.

Do not copy:

- Do not become a generic AI app builder.
- Do not optimize for chatbot publishing before employee operations are stable.

### Flowise

Flowise is useful for visual low-code orchestration and node-based LLM workflow building.

Borrow:

- Visual graph editing patterns.
- Node configuration drawers.
- Quick workflow prototyping.

Do not copy:

- Do not reduce employee work to stateless prompt-chain nodes.
- Node success is not business delivery unless evidence is registered and accepted.

### kagent

kagent is relevant for future infrastructure and Kubernetes-native agent operations. It is less important for the local desktop-first stage, but it provides useful thinking for multi-node execution and policy-controlled infrastructure operations.

Borrow:

- Kubernetes-native runtime model for future distributed nodes.
- Infra operation safety patterns.
- Declarative agent deployment concepts.

Do not copy:

- Do not require Kubernetes for first-phase users.
- Do not let infra complexity block local employee workflow.

### Idun Platform

Idun is relevant as an agent observability, evaluation, and governance reference. It points toward enterprise concerns: monitoring, evaluation, policy, and risk visibility.

Borrow:

- Risk and evaluation dashboard language.
- Safety monitoring.
- Governance as an operational layer, not a one-off setting.

Do not copy:

- Do not make governance only post-hoc reporting.
- The kernel must enforce approvals and policy before risky actions execute.

## Key Questions Answered

### Does the same product already exist?

Partially, but not fully.

- Agent workspace products exist.
- Multi-agent frameworks exist.
- LLM observability products exist.
- Workflow builders exist.
- Coding-agent runtimes exist.
- Governance/evaluation tools exist.

But a durable AI employee company kernel that combines employee registry, task ledger, long-running attempts, budget, approval, evidence, handoff, audit, runtime adapters, and future skill marketplace is still a differentiated direction.

### Difference vs Paperclip / Multica

Paperclip and Multica are strong product-experience references. They appear closer to "AI agent workspace" and "agent company UI".

Super AI Company Kernel should be different in the backend:

- Paperclip/Multica-like UX: connect agents, choose employees, assign tasks.
- Kernel-level difference: every task is durable, auditable, cancellable, retryable, budgeted, and evidence-bound.
- Future difference: employees/skills can become rentable assets only if the kernel can prove what happened, what it cost, and what was delivered.

### Are we missing backend control plane?

Partially.

Existing kernel already has many control-plane components: employees, runtimes, tasks, messages, events, heartbeats, locks, approvals, adapter runs, workspaces, artifacts, evidence, handoffs, attempts, and context packages.

Missing 2.0 pieces:

- Structured tool-call ledger.
- Budget/cost/token/compute ledger.
- Runtime session model.
- Policy/risk engine.
- CEO timeline/replay API.
- Goal/KPI/WorkGraph model.

### Are we missing execution trace?

Partially.

Trace exists in tasks, events, adapter runs, artifacts, evidence, handoffs, and attempts, but the product needs a unified trace view that answers:

- Who did what?
- In which attempt?
- With which tool call?
- Which artifact was created?
- Which evidence was promoted?
- What did it cost?
- Who approved it?
- Why did it fail, retry, or get reassigned?

### Are we missing cost budget?

Yes.

Current architecture needs a Budget Center:

- Token usage.
- Model usage.
- API cost.
- Local compute time.
- Human approval cost or delay.
- Per employee/task/project budget.
- Budget reservation before execution.
- Budget spend events during execution.
- Budget exceeded events and approvals.

### Are we missing approval governance?

Partially.

Approvals exist, but 2.0 needs stronger governance:

- Risk policy table.
- Approval routing rules.
- Expiry and escalation.
- Owner-only actions.
- Dry-run default for external send/payment/destructive actions.
- Evidence requirement before approval completion.
- Auditable approval reason and actor.

### Are we missing agent ability?

Not primarily.

The system already has multiple agents and adapters. The bigger gap is not raw agent ability; it is reliable management of ability:

- Can this employee accept this task type?
- Can it provide progress?
- Can it submit matching evidence?
- Can it be corrected?
- Can it be cancelled?
- Can it obey workspace boundaries?
- Can it be charged/budgeted?
- Can it be evaluated and promoted from candidate to active?

### Bottom layer first or product layer first?

Bottom layer first, with a thin but real CEO Dashboard.

Reason:

- Marketplace and hiring UX require trust.
- Trust requires trace, evidence, cost, approvals, and replay.
- Without the ledger, marketplace disputes cannot be resolved.
- Without budget, users cannot rent or sell skill time safely.
- Without evidence, employees cannot prove delivery.

Recommended order:

1. Strengthen kernel ledger and APIs.
2. Expose CEO Dashboard from real ledger state.
3. Add better UX inspired by Paperclip/Multica.
4. Add skill marketplace only after evidence and budget are stable.

## Super AI Company Kernel 2.0 Positioning

Super AI Company Kernel 2.0 should be positioned as:

> The local-first AI employee operating system: a control plane that registers AI employees, assigns long-running work, supervises progress, records cost, enforces approvals, stores evidence, and shows the owner exactly what happened.

It is not:

- A generic chatbot.
- A pure multi-agent demo.
- A prompt-chain builder.
- A coding-agent IDE only.
- A marketplace first.
- A pure observability tool.

It is:

- AI employee registry.
- Task and attempt ledger.
- Runtime adapter layer.
- Evidence and artifact store.
- Budget and cost center.
- Approval and risk governance.
- WorkGraph and KPI system.
- CEO operations cockpit.

## 2.0 Core Modules

| Module | Purpose | Current Status | 2.0 Work |
|---|---|---|---|
| Agent Registry | Register employees, runtime type, capabilities, status | Exists | Add activation levels and ability certification |
| Runtime Sessions | Track live CLI/GUI/local/Docker sessions | Partial through runtimes/heartbeats | Add session_id, pid, window/session key, lease, shutdown state |
| Task Ledger | Durable task state machine | Exists | Add clearer long-task policy and stagnant/correction states |
| Execution Attempts | One attempt per run/retry/reassign | Exists | Enforce attempt-bound evidence and cancellation rules |
| Agent Events | Employee/task/event stream | Exists | Normalize all critical actions as events |
| Tool Calls | Structured tool execution ledger | Missing | Add first-class table and API |
| Artifact Registry | File outputs and versions | Exists | Strengthen validation, usage tracking, and lineage UI |
| Evidence Store | Accepted proof of delivery | Exists | Add validation status and acceptance workflow |
| Handoff | Employee-to-employee transfer contract | Exists | Make downstream acceptance/rejection visible |
| Budget Center | Cost/token/compute budget | Missing | Add budget accounts, spend events, reservations, thresholds |
| Approval Center | Human approval and risk gates | Exists basic | Add risk policy, routing, expiry, escalation |
| Audit Log | Who/what/when/why | Exists | Link audit records to trace/task/attempt/tool/evidence |
| CEO Dashboard | Owner-readable operations cockpit | Partial | Build from real ledger: cost, blockers, evidence, throughput |
| WorkGraph | Goal/task/handoff dependency graph | Partial | Add goals, KPI, graph nodes/edges, replay |

## Data Model Additions

The existing schema should be extended rather than replaced.

### runtime_sessions

Tracks live long-running runtimes independently from employee identity.

Fields:

- session_id
- employee_id
- adapter_type
- runtime_type
- pid
- session_key
- status: starting / active / idle / stale / stopping / stopped / failed
- started_at
- last_heartbeat_at
- last_progress_at
- stopped_at
- metadata_json

### agent_tool_calls

Every meaningful tool or external action should be recorded.

Fields:

- tool_call_id
- trace_id
- task_id
- attempt_id
- employee_id
- session_id
- tool_name
- tool_type: shell / browser / file / api / openclaw / telegram / local_script / docker / model / other
- input_summary
- input_json
- output_summary
- output_json
- status: planned / running / success / failed / blocked / cancelled
- risk_level
- approval_id
- started_at
- finished_at
- error_message

### budget_accounts

Defines budget scope.

Fields:

- budget_account_id
- scope_type: owner / project / task / employee / skill
- scope_id
- currency
- hard_limit
- soft_limit
- period
- status
- created_at
- metadata_json

### budget_events

Records actual and estimated spend.

Fields:

- budget_event_id
- budget_account_id
- trace_id
- task_id
- attempt_id
- employee_id
- event_type: reserved / spent / released / exceeded / adjusted
- cost_type: token / model_api / local_compute / external_api / human_review / other
- amount
- currency
- token_input
- token_output
- model_name
- provider
- created_at
- metadata_json

### risk_policies

Defines what requires approval.

Fields:

- policy_id
- name
- action_pattern
- risk_level
- requires_approval
- approval_role
- default_mode: allow / dry_run / block
- evidence_required
- enabled
- created_at
- metadata_json

### goals

Business-level objective above tasks.

Fields:

- goal_id
- title
- description
- owner_id
- status: proposed / active / blocked / completed / cancelled
- priority
- target_metric
- current_metric
- due_at
- created_at
- updated_at
- metadata_json

### workgraph_nodes and workgraph_edges

Makes dependencies, handoffs, and goal relationships visible.

Node fields:

- node_id
- trace_id
- node_type: goal / project / task / attempt / artifact / evidence / approval / blocker
- ref_id
- status
- label
- metadata_json

Edge fields:

- edge_id
- trace_id
- from_node_id
- to_node_id
- edge_type: depends_on / produces / consumes / approves / blocks / retries / reassigns / handoff
- created_at
- metadata_json

### replay_snapshots

Allows timeline reconstruction.

Fields:

- replay_snapshot_id
- trace_id
- task_id
- attempt_id
- snapshot_type
- summary
- state_json
- created_at

## API Plan

Keep top-level API small and grouped by control-plane concepts.

| API | Purpose |
|---|---|
| `GET /v1/ceo-dashboard` | One owner-readable snapshot: employees, running tasks, blockers, approvals, spend, evidence |
| `GET /v1/traces/{trace_id}/timeline` | Unified timeline of events, attempts, tool calls, artifacts, evidence, approvals |
| `GET /v1/traces/{trace_id}/workgraph` | Graph nodes/edges for visual view |
| `GET /v1/tasks/{task_id}/attempts` | Attempt history |
| `POST /v1/tasks/{task_id}/corrections` | Owner/Hermes correction to running attempt |
| `POST /v1/tasks/{task_id}/cancel` | Cancel task/attempt safely |
| `POST /v1/tool-calls` | Adapter records a tool call |
| `PATCH /v1/tool-calls/{tool_call_id}` | Complete/fail/block tool call |
| `GET /v1/budgets` | Budget accounts and current spend |
| `POST /v1/budget-events` | Record reservation/spend/release |
| `GET /v1/approvals/queue` | Owner approval queue |
| `POST /v1/approvals/{approval_id}/approve` | Approve with actor/reason |
| `POST /v1/approvals/{approval_id}/reject` | Reject with actor/reason |
| `GET /v1/evidence/{evidence_id}/safe-preview` | Safe evidence preview from whitelist only |
| `GET /v1/goals` | Business goals and KPI status |
| `POST /v1/goals` | Create goal |
| `GET /v1/employee-matrix` | Readiness levels and failure reasons |

Rules:

- Dashboard/API/CLI must read the same ledger.
- Dashboard must not directly read private employee folders.
- Evidence preview must enforce whitelist, relative paths, and no secret paths.
- All mutating endpoints must write company_events and audit_logs.

## Frontend Pages

Use five main navigation areas.

### 1. CEO Cockpit

First screen for the owner.

Must show:

- Employee health: online, busy, candidate, active-limited, unsafe.
- Running tasks.
- Stagnant tasks.
- Blocked tasks.
- Pending approvals.
- Recent evidence.
- Spend today / this week.
- Hermes supervisor corrections.
- Delivery throughput.

Main question answered: "What is my AI company doing right now?"

### 2. Tasks and Workflows

Task operations page.

Must show:

- Task tree.
- Status state machine.
- Attempt history.
- Progress freshness.
- Retry/reassign/cancel/correct actions.
- Parent/child dependencies.

Main question answered: "Where is this task stuck or completed?"

### 3. AI Fleet and Skills

Employee and skill management.

Must show:

- Employee status.
- Activation level: active_ready / active_limited / candidate_only / online_only / task_unsupported / no_reply / unsafe.
- Runtime session.
- Capabilities.
- Supported task types.
- Evidence certification history.
- Skill packages.

Main question answered: "Who can do what, and can I trust them?"

### 4. Chat Hub

Task-bound communication, not random chat.

Must show:

- Conversations grouped by task/trace.
- Direct messages with receipts.
- Supervisor corrections.
- Agent replies.
- Hidden greeting/handshake/idle chatter by default.

Main question answered: "What did employees say about this task?"

### 5. Audit, Approvals, Evidence

Governance and proof page.

Must show:

- Approval queue.
- Risk policy hits.
- Audit log.
- Artifact lineage.
- Evidence validation.
- Handoff accepted/rejected.
- Failure/retry history.

Main question answered: "Can I accept this result, and who is accountable?"

## Event and State Machine Plan

### Task States

Recommended states:

- queued
- claimed
- starting
- running
- stagnant
- correcting
- waiting_approval
- blocked
- stale
- retrying
- failed
- cancelling
- cancelled
- done

Definitions:

- `stagnant`: employee heartbeat is fresh, but task progress is old.
- `stale`: employee/session heartbeat is missing or expired.
- `blocked`: employee reported a concrete blocker or supervisor classified it.
- `failed`: attempt ended with failure evidence.
- `done`: final evidence exists and matches task_id/attempt_id.

### Attempt States

- starting
- running
- stagnant
- correcting
- blocked
- stale
- cancelling
- cancelled
- success
- failed

### Required Event Types

Existing event types should continue. Add or normalize:

- runtime.session.started
- runtime.session.heartbeat
- runtime.session.stale
- runtime.session.stopped
- tool.call.started
- tool.call.completed
- tool.call.failed
- tool.call.blocked
- budget.reserved
- budget.spent
- budget.released
- budget.exceeded
- approval.routed
- approval.expired
- supervisor.correction_requested
- supervisor.correction_acknowledged
- task.stagnant
- task.cancel_requested
- task.cancel_confirmed
- evidence.validated
- evidence.rejected
- goal.created
- goal.updated
- goal.completed
- workgraph.edge.created

## Runtime Adapter Plan

The kernel should remain runtime-agnostic.

| Adapter | 2.0 Requirement |
|---|---|
| Local Script | Must be deterministic test baseline for task/progress/evidence/budget/tool-call events |
| Codex | Must support long-running attempts, progress, correction, cancel, evidence, git/test summaries |
| Hermes | Default supervisor; reads ledger and issues corrections/blocker summaries |
| Antigravity | Design-review/GUI/front-end worker until it proves structured execution evidence |
| OpenClaw | Bridge only; do not modify private bus; all critical state goes through kernel adapter |
| Claude/Gemini | Candidate/active-limited until they prove task execution and evidence |
| Docker Skill | Future rentable skill runtime with declared input/output schema and permissions |
| JiuwenSwarm | Future adapter; no kernel dependency |

Activation rule:

- Online is not active.
- Chat reply is not task support.
- Task completion requires task_id/attempt_id-bound evidence.
- Candidate becomes active only after structured progress, output, evidence, and supervisor validation.

## Evidence Layer

Evidence is the product's trust layer.

Rules:

- Every completed task must have final evidence.
- Evidence must bind to task_id, attempt_id, employee_id, trace_id.
- Evidence must come from registered artifact or verified external proof.
- Final artifact must be promoted to evidence.
- Cancelled or superseded attempts cannot submit final evidence.
- Rejected/superseded artifacts do not enter downstream context by default.
- Evidence preview only reads whitelisted workspace paths.

Whitelist policy:

- Allow only project-controlled workspace/evidence/reports/artifacts/final paths.
- Store relative paths.
- Block absolute path preview.
- Block `../`.
- Block `.env`, `config`, `profile`, `api key`, `token`, `~/.ssh`, credential files, and private system files.

## Budget Center

Budget Center is required before marketplace.

Questions it must answer:

- How much did this employee spend?
- How much did this task spend?
- Which model/API/tool created the cost?
- Did the task exceed budget?
- Was spend approved?
- Which skill is profitable or wasteful?

P0 budget can start with estimates:

- model_name
- provider
- token_input
- token_output
- estimated_cost
- local_runtime_seconds
- external_api_name
- external_api_cost

P1 budget should add:

- reservation before execution.
- soft/hard limits.
- spend alerts.
- approval on exceed.
- per project/employee/skill rollups.

## Approval Center

Approval is not just a button; it is a governance contract.

Approval should be triggered by:

- external send.
- payment or money-related action.
- destructive file/system action.
- public posting.
- customer-facing message.
- rule/protocol change.
- budget exceed.
- sensitive evidence access.
- task marked done without sufficient validation.

Every approval must record:

- approval_id
- task_id
- attempt_id
- trace_id
- employee_id
- action_type
- risk_level
- requested_payload
- evidence_required
- status
- requested_at
- approved_by/rejected_by
- decision_reason
- decided_at

## Audit Log

Audit should answer:

- Who changed state?
- Was it a human, supervisor, adapter, daemon, or worker?
- What changed?
- Which task/attempt/trace/evidence was affected?
- What was the before/after summary?
- Was there approval?
- Was there a policy hit?

Audit log should be linked to:

- task
- attempt
- employee
- session
- tool_call
- approval
- artifact
- evidence
- budget_event
- goal

## CEO Dashboard Metrics

Top metrics:

- Active employees.
- Candidate employees.
- Running tasks.
- Stagnant tasks.
- Blocked tasks.
- Pending approvals.
- Today spend.
- This week spend.
- Evidence awaiting review.
- Completed tasks today.
- Average cycle time.
- Retry rate.
- Handoff rejection rate.
- Supervisor corrections issued.
- Unsafe actions blocked.

CEO-level wording examples:

- "Codex is still online, but task progress has not changed for 14 minutes. Options: wait, probe, correct, cancel."
- "Antigravity replied, but has not submitted task-bound evidence. Status remains candidate."
- "ecommerce-copy-skill cannot chat, but can run tasks and submit evidence. Chat action hidden; progress remains visible."
- "This task spent 18,200 tokens and produced 3 artifacts. Final evidence is awaiting review."

## Phased Development Plan

### Phase 1: Agent Registry, Task Ledger, Agent Events, Tool Calls, Audit Log

Goal: make every agent action observable and durable.

Deliverables:

- Add runtime_sessions.
- Add agent_tool_calls.
- Normalize task/attempt/event relationships.
- Add tool-call API.
- Emit tool-call events from Local Script and Codex paths first.
- Add dashboard timeline that merges events, attempts, tool calls, artifacts, evidence.
- Add employee activation level to dashboard.

Acceptance:

- A task can show all attempts and tool calls.
- A cancelled attempt cannot later mark done.
- Skill worker without chat does not show chat action.
- Every meaningful adapter action writes event and audit record.

### Phase 2: Budget Center, Approval Queue, Risk Policy

Goal: make long-running work financially and operationally controllable.

Deliverables:

- Add budget_accounts and budget_events.
- Add estimated token/cost recording.
- Add risk_policies.
- Route high-risk actions to approval.
- Add Budget Center cards to CEO Dashboard.
- Add approval expiry/escalation.

Acceptance:

- Owner can see spend per task/employee/project.
- Exceeding budget creates approval or block.
- External send/destructive actions default to dry-run unless approved.

### Phase 3: Evidence Store, Timeline, Replay, Dashboard

Goal: make completed work provable and reviewable.

Deliverables:

- Evidence safe preview endpoint.
- Evidence validation status.
- Trace timeline API.
- Replay snapshots.
- Artifact lineage view.
- Handoff accepted/rejected UI.

Acceptance:

- Owner can replay a task from created to done.
- Final evidence is previewable safely.
- Rejected/superseded artifacts do not flow downstream by default.

### Phase 4: Goal System, KPI, WorkGraph

Goal: move from tasks to business operating system.

Deliverables:

- Add goals.
- Add workgraph_nodes and workgraph_edges.
- Add KPI fields.
- Add goal-to-task decomposition.
- Add WorkGraph Explorer page.
- Add milestone slippage and blocker topology.

Acceptance:

- CEO can see which tasks support which goal.
- Blockers and evidence are visible on the graph.
- WorkGraph can drive task creation and handoffs.

### Phase 5: Multi-agent Protocol, Skill, Memory, Long-term Autonomy

Goal: prepare for AI talent marketplace.

Deliverables:

- Skill package certification flow.
- Employee ability tests.
- Shared SOP/RAG memory injection.
- Cross-agent verification.
- Runtime sandbox profiles.
- Skill pricing metadata.
- Dispute/review evidence package.

Acceptance:

- A skill can declare input/output/permissions/pricing.
- Kernel can run certification task and grade evidence.
- Future marketplace can use trace/evidence/budget for billing and dispute resolution.

## Seven-Day Execution Plan

### Day 1

Freeze 2.0 control-plane RFC.

- Create schema proposal for runtime_sessions, agent_tool_calls, budget_accounts, budget_events, risk_policies, goals, workgraph_nodes, workgraph_edges.
- Decide which fields are P0 migrations and which are TODO.
- Do not touch OpenClaw private bus.

### Day 2

Implement tool-call ledger foundation.

- Add agent_tool_calls table.
- Add create/update/list API.
- Add audit/event emission.
- Instrument Local Script adapter first.

### Day 3

Runtime session and cancellation hardening.

- Add runtime_sessions table/API.
- Bind attempts to runtime sessions.
- Ensure cancelled attempts cannot submit final evidence.
- Add dashboard session status.

### Day 4

Budget Center MVP.

- Add budget_accounts and budget_events.
- Add estimated token/cost fields.
- Add task/employee/project spend summaries.
- Add CEO Dashboard budget cards.

### Day 5

Approval and risk policy MVP.

- Add risk_policies.
- Route external_send/destructive/budget_exceeded actions to approvals.
- Add expiry/escalation fields.
- Add dashboard approval action audit.

### Day 6

Trace timeline and safe evidence preview.

- Add unified trace timeline endpoint.
- Merge events, attempts, tool calls, artifacts, handoffs, evidence, approvals, budget.
- Add safe evidence preview.
- Add task drawer timeline.

### Day 7

End-to-end local validation.

- Run Local Script ecommerce package demo.
- Run Codex worker task with progress/evidence.
- Run Antigravity design-review task but keep candidate unless it submits structured execution evidence.
- Verify dashboard shows real data.
- Verify doctor summary.
- Commit and push.

## Agy Review Summary

Agy reviewed the direction as a design/product reviewer and emphasized:

- Paperclip/Multica are closer to agent workspace and orchestration UX.
- Super AI Company Kernel should be an enterprise AI operations kernel, not a session-only workspace.
- The kernel should separate company policy from runtime mechanism.
- The strongest 2.0 gaps are dynamic WorkGraph, policy/sandbox guard, corporate SOP/memory, and cross-agent verification.
- CEO Dashboard should show throughput, cost/ROI, blockers, approvals, employee health, artifact rejection, resource lock contention, and milestone risk.
- Do not copy low-code stateless workflow builders, black-box multi-agent chat, pure LLM trace dashboards, or single-runtime workspace ownership.

This review aligns with the architecture above.

## Source Links

Primary sources used for research:

- Langfuse documentation: https://langfuse.com/docs
- Langfuse GitHub: https://github.com/langfuse/langfuse
- VoltAgent documentation: https://voltagent.dev/docs/
- VoltAgent GitHub: https://github.com/VoltAgent/voltagent
- OpenHands documentation: https://docs.all-hands.dev/
- OpenHands GitHub: https://github.com/All-Hands-AI/OpenHands
- AutoGen Studio documentation: https://microsoft.github.io/autogen/stable/user-guide/autogenstudio-user-guide/index.html
- AutoGen GitHub: https://github.com/microsoft/autogen
- CrewAI documentation: https://docs.crewai.com/
- CrewAI GitHub: https://github.com/crewAIInc/crewAI
- LangGraph documentation: https://langchain-ai.github.io/langgraph/
- Dify documentation: https://docs.dify.ai/
- Dify GitHub: https://github.com/langgenius/dify
- Flowise documentation: https://docs.flowiseai.com/
- Flowise GitHub: https://github.com/FlowiseAI/Flowise
- kagent website: https://kagent.dev/
- kagent GitHub: https://github.com/kagent-dev/kagent
- Idun Platform website: https://idunplatform.com/

Note: Paperclip and Multica were rechecked again on 2026-06-09. The most important verified details are summarized below; they should replace any earlier uncertain assumptions in this document.

## 2026-06-09 Secondary Verification And MVP Mapping

This section narrows the plan from a broad 2.0 architecture into a practical 3-day backend-first MVP. The goal is not more concept writing. The goal is to make the owner cockpit truthful: employee status, task timeline, tool calls, cost, evidence, approvals, and trace must come from the Company Kernel ledger.

### Paperclip Verification

Verified public positioning:

- Paperclip presents itself as an "AI company control plane".
- It is open source and references GitHub directly.
- It describes local deployment with `docker compose up`.
- It uses a Node.js + React application structure in its public technical description.
- It explicitly mentions AI company hierarchy, tasks, tool-call tracing, budget management, role-based access control, immutable audit log, governance, and a real-time dashboard.

Product interpretation:

- Paperclip is closer to our target than generic agent frameworks because it already uses the "AI company" control-plane language.
- Its strongest reference value is the combination of dashboard, governance, audit, tool-call tracing, and budget, not only the visual UI.
- We should treat Paperclip as a direct product benchmark for the control-plane layer.

What to borrow:

- "Company control plane" framing.
- Budget and governance as first-class product surfaces.
- Tool-call trace visible in the dashboard.
- Immutable audit log language.
- Role/access control framing for employees.

What not to copy blindly:

- Do not assume its model fits our local OpenClaw/Codex/Hermes runtime reality.
- Do not copy architecture before checking how durable its ledger is.
- Do not replace our Company Kernel schema with a Paperclip-shaped app schema.

### Multica Verification

Verified public positioning:

- Multica is open source.
- It supports self-hosting.
- It is built around a UI, API server, daemon, PostgreSQL, and runtime adapters.
- It describes 12 AI agent runtimes, including Codex, Claude Code, Gemini CLI, Cursor, OpenCode, Aider, Auggie CLI, and local agents.
- It includes task lifecycle management, real-time progress through WebSocket, Kanban-style UI, hierarchical task breakdown, artifact collection, CLI integration, and hooks.
- It explicitly targets running multiple coding agents in parallel and tracking their work.

Product interpretation:

- Multica is very close to the experience layer we want: connect agents, see tasks, monitor progress, and collect artifacts.
- It is less clear from public material whether it has the same evidence-governance-budget depth we need for a future AI employee marketplace.
- For our project, Multica should be treated as the closest UX/runtime orchestration benchmark.

What to borrow:

- Agent runtime adapter model.
- Self-hosted UI + API + daemon split.
- Kanban/task lifecycle UX.
- Real-time progress updates.
- Artifact collection as visible output.
- Multi-agent coding workflow patterns.

What not to copy blindly:

- Do not optimize only for coding agents.
- Do not let runtime progress replace evidence acceptance.
- Do not treat WebSocket progress as durable audit.
- Do not delay our budget/approval/evidence model until after UI polish.

### Paperclip / Multica vs Super AI Company Kernel

| Area | Paperclip | Multica | Our Direction |
|---|---|---|---|
| Core framing | AI company control plane | Multi-agent coding/workflow orchestrator | AI employee operating kernel |
| Open source | Yes | Yes | Yes |
| Local deploy | Docker compose described | Self-host described | Local-first, launchd/API/dashboard |
| Agent runtimes | Control-plane oriented | Strong runtime adapter list | Must support OpenClaw/Codex/Hermes/Agy/skills |
| Tasks | Yes | Strong lifecycle/Kanban | Durable task + attempt ledger |
| Tool calls | Explicit tool-call trace | Runtime/action tracking | First-class `agent_tool_calls` ledger |
| Budget | Explicit budget management | Not the main verified strength | Must be P1 MVP |
| Audit | Immutable audit mentioned | Logs/progress/artifacts | `audit_logs` + event ledger + evidence |
| Evidence | Needs deeper verification | Artifacts collected | Evidence is acceptance gate |
| Dashboard | Real-time dashboard | Web UI + Kanban | CEO Cockpit from real API/DB |
| Marketplace readiness | Unknown | Not primary | Future skill renting needs budget/evidence/audit |

Conclusion: a similar direction exists, but our differentiation remains valid if we become evidence-first and owner-governed. If we only build a good-looking multi-agent dashboard, we will be weaker than Paperclip/Multica. If we build a durable employee ledger with budget, approvals, evidence, and replay, we have a stronger foundation for the AI talent marketplace.

## Current Repository Mapping

### Existing / Reusable / Missing

| Capability | Current Status | Existing Files | Reuse | Missing / Next |
|---|---|---|---|---|
| Employee Registry | Exists | `company_kernel/schema.sql`, `company_kernel/companyctl.py`, `/v1/employees`, `/v1/agent-matrix` | Reuse employee table, runtime, status, readiness checks | Add clearer certification history later |
| Employee Readiness | Exists | `companyctl.py`, `company_dashboard.py`, tests around `active_ready`, `candidate_only`, skill no-chat | Reuse current readiness logic | Surface more prominently in cockpit cards |
| Task Ledger | Exists | `tasks`, `task_metadata`, task CLI/API | Reuse | Normalize status names gradually |
| Execution Attempts | Exists | `execution_attempts`, `task run/progress/correct/cancel/retry/reassign` | Reuse | Bind every real adapter run to attempt_id |
| Runtime Sessions | Added / P0 foundation | `runtime_sessions`, `runtime session start/heartbeat/stop/list`, `/v1/runtime-sessions` | Use for long-running CLI/GUI/daemon visibility | Integrate adapters automatically |
| Tool Call Ledger | Added / P0 foundation | `agent_tool_calls`, `tool-call start/finish/list`, `/v1/tool-calls` | Use for shell/API/browser/file/model operations | Auto-instrument Codex/Agy/OpenClaw adapters |
| Agent Events | Exists | `company_events`, `/v1/events`, SSE | Reuse | Ensure all new actions emit events |
| Audit Log | Exists | `audit_logs`, `audit()` helper | Reuse | Add richer actor/source classification later |
| Evidence Store | Exists | `artifacts`, `evidence`, `handoffs`, safe evidence preview | Reuse | Add validation/acceptance status later |
| Trace Timeline | Exists and extended | `company_trace.py`, `/v1/traces/{trace_id}/timeline` | Reuse | Ensure timeline includes runtime sessions, tool calls, budget events |
| CEO Dashboard | Partial but real | `company_dashboard.py`, `dashboard_templates/gemini_dashboard.html`, `/v1/dashboard/cockpit` | Reuse | Add visible tool-call/runtime/budget cards |
| Approval Center | Exists basic | `approvals`, `/v1/approvals`, dashboard approval actions | Reuse | Add risk policy and budget exceed routing |
| Budget Center | Missing | No budget tables yet | None | Add `budget_accounts`, `budget_events` MVP |
| Cost Records | Missing | No token/cost table yet | Tool-call metadata can bridge temporarily | Add first-class token/cost fields |
| Marketplace | Not for MVP | N/A | Do not build now | Only keep skill package metadata |

### Existing Schema Anchors

Already present:

- `employees`
- `tasks`
- `heartbeats`
- `approvals`
- `audit_logs`
- `company_events`
- `adapter_runs`
- `task_workspaces`
- `artifacts`
- `evidence`
- `handoffs`
- `execution_attempts`
- `task_context_packages`
- `runtime_sessions`
- `agent_tool_calls`

Need next:

- `budget_accounts`
- `budget_events`
- Optional later: `risk_policies`, `goal_records`, `workgraph_nodes`, `workgraph_edges`

### Existing API Anchors

Already present or newly added:

- `GET /v1/dashboard/cockpit`
- `GET /v1/traces/{trace_id}/timeline`
- `GET /v1/runtime-sessions`
- `GET /v1/tool-calls`
- `GET /v1/evidence`
- `GET /v1/artifacts`
- `GET /v1/handoffs`
- `GET /v1/approvals`
- `GET /v1/agent-matrix`
- `POST /v1/tasks/{task_id}/run`
- `POST /v1/tasks/{task_id}/progress`
- `POST /v1/tasks/{task_id}/correct`
- `POST /v1/tasks/{task_id}/cancel`
- `POST /v1/tasks/{task_id}/retry`
- `POST /v1/tasks/{task_id}/reassign`

Need next:

- `GET /v1/budget-events`
- `POST /v1/budget-events`
- `GET /v1/budget-summary`
- Optional later: `GET /v1/risk-policies`, `POST /v1/risk-policies`

### Existing CLI Anchors

Already present or newly added:

- `companyctl employee list`
- `companyctl agent-matrix`
- `companyctl task run`
- `companyctl task progress`
- `companyctl task correct`
- `companyctl task cancel`
- `companyctl task retry`
- `companyctl task reassign`
- `companyctl runtime session start`
- `companyctl runtime session heartbeat`
- `companyctl runtime session stop`
- `companyctl runtime session list`
- `companyctl tool-call start`
- `companyctl tool-call finish`
- `companyctl tool-call list`
- `companyctl trace timeline`
- `companyctl audit evidence`
- `companyctl approval list`

Need next:

- `companyctl budget record`
- `companyctl budget summary`

### Existing Dashboard Anchors

Existing files:

- `company_kernel/company_dashboard.py`
- `dashboard_templates/gemini_dashboard.html`
- `bin/company-dashboard`
- `bin/company-dashboard-server`

Already usable:

- Employee readiness cards.
- Skill worker no-chat treatment.
- Long task state.
- Owner attention.
- Evidence safety display.
- Trace telemetry.
- Approval actions.

Need next:

- Visible Runtime Session card.
- Visible Tool Call ledger panel.
- Budget summary card.
- Task detail drawer should include tool calls and cost events.

## Phase 1 MVP Scope

Phase 1 must be small enough to complete and verify quickly.

### MVP Features

| MVP Feature | Requirement | Status |
|---|---|---|
| Employee status | Show active/candidate/online_only/task_unsupported accurately | Existing |
| Task timeline | Show task, event, attempt, artifact, handoff, evidence | Existing |
| Tool-call ledger | Record tool call start/finish and show in trace | Added P0 foundation |
| Runtime session | Track CLI/GUI/script session heartbeat | Added P0 foundation |
| Cost record | Record token/cost/runtime seconds | Next missing MVP |
| Evidence submission | Final task requires evidence | Existing |
| Owner cockpit | Show true counts from DB/API | Existing, needs visible cost/tool panels |

### What Phase 1 Does Not Include

- Marketplace.
- Payment.
- User accounts.
- Distributed node renting.
- Complex graph canvas.
- Full WorkGraph KPI system.
- Heavy frontend redesign.

## File-Level Development Plan

### 1. Budget Center MVP

Files:

- `company_kernel/schema.sql`
- `company_kernel/schema_migrations.py`
- `company_kernel/companyctl.py`
- `company_kernel/api_gateway.py`
- `company_kernel/company_dashboard.py`
- `tests/test_company_kernel_core.py`

New tables:

- `budget_accounts`
- `budget_events`

Minimum fields:

- `budget_account_id`
- `scope_type`
- `scope_id`
- `currency`
- `soft_limit`
- `hard_limit`
- `status`
- `budget_event_id`
- `trace_id`
- `task_id`
- `attempt_id`
- `employee_id`
- `cost_type`
- `amount`
- `currency`
- `token_input`
- `token_output`
- `model_name`
- `provider`
- `runtime_seconds`
- `created_at`
- `metadata_json`

CLI:

- `companyctl budget record --task-id ... --employee ... --amount ... --cost-type ...`
- `companyctl budget summary --task-id ...`

API:

- `GET /v1/budget-events`
- `POST /v1/budget-events`
- `GET /v1/budget-summary`

Dashboard:

- Add `counts.budget_events`, `counts.estimated_cost`.
- Add cockpit `budget_summary`.
- Add task detail cost row.

Tests:

- budget event can be recorded by CLI.
- API returns sanitized budget events.
- cockpit shows task/employee/project cost.
- trace timeline includes budget event.

### 2. Tool Call Auto-Instrumentation

Files:

- `company_kernel/codex_adapter.py`
- `company_kernel/antigravity_adapter.py`
- `company_kernel/openclaw_adapter.py`
- `company_kernel/skill_package_worker.py`
- `company_kernel/companyctl.py`
- `tests/test_company_kernel_core.py`

Requirement:

- Manual tool-call ledger exists, but adapters must emit tool-call records automatically.
- At minimum, adapter command execution should create `tool.call.started` and `tool.call.success/failed`.

Acceptance:

- Running a skill task creates at least one tool call.
- Failed adapter run creates failed tool call.
- Trace timeline shows the tool call.

### 3. Dashboard Visible Control Plane Panels

Files:

- `company_kernel/company_dashboard.py`
- `dashboard_templates/gemini_dashboard.html`
- `tests/test_company_kernel_core.py`

Add visible panels:

- Runtime Sessions.
- Tool Calls.
- Budget.

Rules:

- No fake data.
- No raw secrets/stdout.
- Use `/v1/dashboard/cockpit`, `/v1/runtime-sessions`, `/v1/tool-calls`, `/v1/budget-summary`.

Acceptance:

- Browser/dashboard shows non-empty session/tool/budget data after local demo.
- Skill worker still hides chat action.
- Candidate employee still not promoted by online-only reply.

## 3-Day MVP Plan

### Day 1: Control-Plane Ledger

Goal:

- Finish runtime session and tool-call ledger integration.

Tasks:

- Verify `runtime_sessions` and `agent_tool_calls` migrations.
- Verify CLI/API/trace/cockpit return new records.
- Add adapter auto-instrumentation for skill worker or Codex adapter first.
- Run targeted tests and full unit discovery.

Acceptance commands:

```bash
python3 -m unittest tests.test_company_kernel_core.CompanyKernelCoreTest.test_runtime_sessions_and_tool_calls_are_first_class_trace_records
python3 -m unittest discover -s tests -p 'test*.py'
```

### Day 2: Budget Center MVP

Goal:

- Owner can see basic cost per task/employee.

Tasks:

- Add `budget_accounts` and `budget_events`.
- Add `companyctl budget record/summary`.
- Add `/v1/budget-events` and `/v1/budget-summary`.
- Add budget rollup to `/v1/dashboard/cockpit`.
- Add trace timeline budget event.

Acceptance commands:

```bash
bin/companyctl budget record --task-id task-demo --employee codex --cost-type model_api --amount 0.12 --currency USD --token-input 1000 --token-output 400 --model-name gpt-5
bin/companyctl budget summary --task-id task-demo
curl -s http://127.0.0.1:8780/v1/budget-summary | python3 -m json.tool
python3 -m unittest discover -s tests -p 'test*.py'
```

### Day 3: Real Dashboard Validation

Goal:

- Dashboard shows true employee/task/tool/budget/evidence state from DB.

Tasks:

- Add visible Runtime Sessions / Tool Calls / Budget cards to dashboard template.
- Run local demo task producing progress, tool call, budget event, artifact, evidence.
- Verify skill worker no-chat UI.
- Verify candidate employee is not active_ready without evidence.
- Verify trace detail contains attempt/progress/tool/evidence/budget.

Acceptance commands:

```bash
bin/companyctl doctor --summary
curl -s http://127.0.0.1:8780/v1/dashboard/cockpit | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/tool-calls | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/runtime-sessions | python3 -m json.tool
curl -s http://127.0.0.1:8780/v1/budget-summary | python3 -m json.tool
python3 -m unittest discover -s tests -p 'test*.py'
```

## Immediate Next Implementation Priority

Do not build marketplace or a new UI shell yet.

The next code sprint should implement:

1. Budget Center MVP tables and API.
2. Adapter auto-instrumentation for tool calls.
3. Dashboard visible panels for Runtime Sessions, Tool Calls, and Budget.
4. One local end-to-end demo proving employee -> task -> attempt -> tool call -> budget -> artifact -> evidence -> cockpit.

If this is complete, the product becomes meaningfully more usable: the owner can see not only that an employee is online, but what it did, which tool it used, what it cost, and what evidence it delivered.

## Secondary Verification Source Links

- Paperclip website: https://paperclip-ai.com/
- Multica GitHub: https://github.com/multica-sh/multica
