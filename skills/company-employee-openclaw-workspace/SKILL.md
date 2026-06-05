---
name: company-employee-openclaw-workspace
description: Use when onboarding or operating OpenClaw workspace agents through Company Kernel, especially when agent-to-agent messages, bus tasks, human chat, heartbeat, and multi-round execution loops must be separated and verified.
---

# Company Employee: OpenClaw Workspace

OpenClaw workspace agents are business/runtime agents that often have two different input surfaces:

1. **Human conversation surface**: Telegram/LINE/API chat messages from the owner or customer.
2. **Agent-to-agent work surface**: Company Kernel messages, OpenClaw bus tasks, adapter queue items, and receipts.

Do not assume these surfaces are processed by the same loop. A common failure is that the agent handles the human chat but ignores agent-to-agent inbox/bus messages.

## One-Sentence Onboarding

“Onboard each OpenClaw workspace as a candidate first, prove it can process agent-to-agent tasks separately from human chat, then activate it only after it returns multi-round receipts and execution evidence.”

## Message Types Are Not Equivalent

- `companyctl message send`: record-only Company Kernel message. It creates history/inbox, but does not prove the OpenClaw agent processed it.
- `companyctl message direct`: synchronous runtime probe when supported. It proves immediate reply capability only.
- `companyctl task submit` + adapter: execution request. It should produce claim/progress/done/block evidence.
- OpenClaw `ops/agent_bus/inbox/<agent>/...`: legacy task queue. It requires a bus worker or the target agent’s heartbeat/work loop to drain it.
- Human Telegram/LINE chat: usually routed by gateway/session logic. It may not scan internal agent inboxes at all.

## Why Multi-Round Communication Fails

Most failures come from one of these root causes:

1. **Record-only vs execution confusion**
   - Agents “communicated” because a message file/DB row exists.
   - But no worker invoked the target runtime, so the target never acted.

2. **Human chat loop starves internal tasks**
   - The agent runtime is busy responding to the human-originated chat.
   - Its scheduler does not also poll Company Kernel messages or OpenClaw bus tasks.
   - Result: the owner sees replies, but agent-to-agent messages sit unprocessed.

3. **No task contract in the message**
   - The receiver gets prose, not a machine-actionable task.
   - It may acknowledge, summarize, or ignore instead of executing.
   - A valid agent-to-agent execution request must include: task id, requested action, allowed scope, expected evidence, reply target, deadline/ETA, and blocker format.

4. **Missing claim/progress/final receipt state machine**
   - If the target does not claim the task, no one knows it is working.
   - If it claims but never sends progress, the sender thinks it stalled.
   - If it completes locally but does not send final receipt, the human never hears back.

5. **Wrong reply surface**
   - The target replies to its own chat, local file, or generic outbox instead of the source agent.
   - Human-facing loops require: human -> requesting agent -> target agent -> requesting agent -> human.

6. **Policy/approval blockers become silence**
   - A safe policy blocks execution, but the blocker is not returned to the sender.
   - This looks like “agent ignored the request.”

7. **Status is over-trusted**
   - `heartbeat`, `active`, `online`, app installed, or simple ACK are not execution proof.
   - Execution proof is evidence path + changed files/output + verification result + final receipt.

## Required Processing Loop

Every OpenClaw workspace agent must implement or be wrapped by this loop:

```text
poll -> classify -> claim -> execute-or-block -> progress -> final receipt -> notify source
```

Required states:

- `received`: message/task discovered.
- `claimed`: target accepts ownership.
- `working`: target has started and says what it is doing.
- `blocked`: target cannot proceed; must include blocker, tried command, evidence path, next required action.
- `done`: target completed; must include evidence path and verification.
- `not_for_me`: target refuses because the request is for another agent; must suggest the correct `@agent` if known.

Preferred 5-layer progress protocol for supervisor heartbeat/API:

- `received` -> `received|acknowledged|claimed`
- `working` -> `working|in_progress|actively_progressing`
- `waiting` -> `waiting|blocked_on_input_or_dependency`
- `blocked` -> `blocked|failed_to_progress`
- `done` -> `done|verified_complete|completed`
- 如果 heartbeat 让 layer/state 发生变化，Kernel 应生成 repo 内 `progress.notification` 记录，并在 delivery 闭环后回写 `delivery_status/delivery_error/delivered_at`；只有 `sent` 才代表真的发给了 Shift。
- 如果没有新的用户催促，也要允许 supervisor loop 主动扫 `progress.notification`；第一次失败可记 `retry_ready`，连续失败再记 `escalate_ready`。

## Minimum Agent-to-Agent Task Envelope

Use this envelope for all internal execution requests:

```json
{
  "task_id": "stable-id",
  "source_agent": "main|hermes|codex|...",
  "target_agent": "agent-id",
  "kind": "read-only|implementation|ops|approval|handoff",
  "human_origin": true,
  "reply_to_agent": "requesting-agent-id",
  "reply_surface": "company-kernel-message|openclaw-bus|telegram|current-conversation",
  "goal": "what must be done",
  "non_goals": ["what must not be done"],
  "allowed_scope": ["absolute/project/path or repo-local area"],
  "verification": ["commands/checks expected"],
  "evidence_required": ["report_path", "stdout_stderr", "exit_code", "changed_files_or_none"],
  "blocker_format": "status/blocker/tried/evidence/next_action",
  "deadline_or_eta": "short ETA or check-in time"
}
```

If a message lacks this envelope and asks for execution, the receiver should return `blocked_missing_task_envelope` instead of guessing.

## Activation Gate

Do not mark an OpenClaw workspace agent as active for autonomous routing until all pass:

1. Human chat still works.
2. Agent-to-agent direct or bus task is processed separately from human chat.
3. The agent returns `claimed` or `blocked` within the expected window.
4. The agent returns final `done` or `blocked` to the source agent.
5. The source agent can notify the human with the result.
6. Evidence path exists and is repo/workspace-local.

## Stabilization Fixes

When agents chat but do not execute, improve these areas:

1. **Separate queues**
   - Keep human chat, direct messages, bus tasks, and scheduled heartbeats as distinct queues.
   - Each queue needs a worker or poller.

2. **Bridge internal messages into the work loop**
   - The agent’s heartbeat should not only say `OK`.
   - It should poll internal inbox/bus tasks, claim one item, and return progress or blocker.

3. **Force receipt on every request**
   - If no receipt is produced, the supervisor should mark `no_receipt` and retry or reroute.

4. **Use direct only for control proof**
   - Direct smoke proves the runtime can reply now.
   - Long work should become a task with progress receipts.

5. **Add watchdog summary**
   - Dashboard should show: pending internal messages, claimed tasks, last progress, final receipts, and stalled workers.

6. **Do not overload human chat context**
   - If the user is talking to the agent, that conversation should not swallow internal tasks.
   - The work loop must read internal queue after each human turn or on heartbeat.

7. **Escalate and reroute after failed follow-up**
   - First missing receipt creates a follow-up to the stalled target.
   - If that follow-up already exists and the watchdog still sees the issue, escalate to Hermes/main.
   - Escalation must include a reroute decision envelope: `continue_original | reroute | block | ask_human`.
   - Reroute decisions must name the candidate new owner, evidence path, rollback, and next action.

## Postmortem: Repeated OpenClaw Internal Communication Bug

Observed pattern:

- Agents appear to talk to each other.
- The receiver has a message in inbox/history.
- But the receiver does not execute the requested action.
- Sometimes the receiver only handles the human’s current chat and ignores agent-to-agent messages.

Root cause:

OpenClaw communication historically mixed notification, chat, and task execution. The system lacked a strict boundary between “message was delivered” and “runtime accepted and executed a task.”

Preventive rule:

No internal request counts as handled until the receiver sends a structured receipt to the source agent. No execution request counts as done until it has evidence and verification. Human chat responsiveness does not imply internal task processing.

## Verification Smoke

Use this after install/repair:

```bash
bin/companyctl task submit \
  --from main \
  --to <openclaw-agent> \
  --task-id smoke-agent-loop-YYYYMMDD-HHMM \
  --title "agent-to-agent loop smoke" \
  --description '{"goal":"reply with claimed then blocked/done","reply_to_agent":"main","evidence_required":["report_path","exit_code","stdout_stderr"]}'

bin/company-openclaw-adapter --agent <openclaw-agent>
bin/companyctl message list --agent main
```

Pass criteria:

- target claims or blocks the task;
- source receives a final receipt;
- evidence path is listed;
- human-facing agent can summarize the result.
