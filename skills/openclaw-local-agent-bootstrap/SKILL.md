---
name: openclaw-local-agent-bootstrap
description: Use when installing or configuring OpenClaw on a customer machine to discover local workspaces, build AI employee models, lock communication surfaces, classify active/candidate/blocked routing, and produce a safe onboarding report before enabling employees.
---

# OpenClaw Local Agent Bootstrap

Use this skill before onboarding or repairing OpenClaw employees in a new local environment. It should work no matter which agent performed the install. Do not make the customer manually reconfigure every employee. Discover, summarize, generate candidate onboarding, ask only for missing facts, then verify.

## Automation Contract

When triggered, do this without waiting for the user to name every employee:

1. Scan local OpenClaw/Company Kernel roots.
2. Discover installed or likely employees from workspaces, runtime commands, app locations, and existing employee files.
3. Summarize each employee's local rules from `AGENTS.md`, `SOUL.md`, `CORE.md`, `SESSION-STATE.md`, `MEMORY.md`, and profile/capability files.
4. Create an onboarding plan with active/candidate/blocked classification.
5. If the user asked to configure automatically, run the scanner with `--apply` so new discoveries become `candidate`, not `active`.
6. Generate direct smoke commands for each candidate.
7. Promote only after direct smoke and owner/routing evidence pass.
8. After configuration, always output the full employee directory and each rename command.
9. Run a 2-4 round employee handshake through the local validation admin. On this Mac, the default validation and approval-flow admin is Hermes.

## Identity and Naming

- Employee id, alias, name, and display_name are all high-priority routing identities.
- Routing lookup priority is: exact id, exact alias, exact name/display_name, then case-insensitive id/name/alias.
- If a user sends to `Codex`, `codex`, or a configured display name, it must resolve to the same canonical employee id.
- If a name/alias is ambiguous, block and ask the user to choose a concrete employee id.
- Users can rename employees with `bin/companyctl employee update --id <id-or-name> --name <new-name>`.
- After every install/apply/repair, print `employee_directory.all` so the user can see all employees, names, statuses, runtimes, and rename commands.

## Fast Path

1. Locate roots:
   - OpenClaw root: user path, `OPENCLAW_ROOT`, `openclaw/`, or CLI path containing `scripts/oc`.
   - Company Kernel root: user path, `OPENCLAW_COMPANY_KERNEL_ROOT`, or repo containing `bin/companyctl`.
   - Workspaces: `workspace-*`, `employees/`, `config/`, `state/`, `skills/`, `docs/`, `scripts/`.
2. Read control files if present:
   - `AGENTS.md`, `SOUL.md`, `CORE.md`, `USER.md`, `SESSION-STATE.md`, `MEMORY.md`.
   - `config/*.json`, `config/*.db`, `company.sqlite`, `state/*.sqlite*`.
   - `employees/*/profile.json`, `employees/*/capabilities.json`, `employees/*/permissions.json`.
   - `docs/RUNTIME_ADAPTERS.md`, `README.md`, project state files.
3. Build each employee model:
   - `identity`: `agent_id`, display name, role, aliases.
   - `runtime`: runtime type, runtime id, workspace, adapter command.
   - `communication`: default reply channel/account/target, session key, direct status.
   - `routing`: internal, business, publish, partner aliases and active/candidate/blocked targets.
4. Classify:
   - `active`: unique source of truth, evidence file, smoke verified.
   - `candidate`: discovered but not confirmed or not smoked.
   - `blocked`: ambiguous, missing, disallowed, or no evidence.
5. Verify direct surface:
   - `bin/companyctl doctor --summary`
   - `bin/companyctl employee list`
   - `bin/companyctl message direct --from main --to <agent> --body "只回复：<agent>_DIRECT_OK"`
   - API equivalent: `POST /v1/messages/direct`.

For a read-only first pass, run the bundled scanner. It discovers existing employees plus likely new employees such as Hermes, Codex, Claude, Trae, Antigravity, OpenClaw workspaces, local models, Cursor, Devin, and Copilot:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root /path/to/openclaw --kernel-root /path/to/company-kernel
```

To create discovered employees as `candidate` entries:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root /path/to/openclaw --kernel-root /path/to/company-kernel --apply
```

Use scanner output as a draft until direct smoke passes. `--apply` must not promote to active.

## Message Semantics

- `message send` / `POST /v1/messages` records an inbox message and emits an event. It does not prove the runtime saw it or replied.
- `message direct` / `POST /v1/messages/direct` invokes the target runtime adapter and can return a runtime reply.
- If a user expects an ACK such as `HERMES_CONFIG_ACK`, use direct smoke or a conversation reply path, not a record-only inbox message.
- Scanner `pending_inbox_messages` means messages are recorded but may still need an adapter, daemon worker, or human/runtime pickup.

## Closed-Loop Coordination

- Every employee request must produce at least one reply/ACK to the sender. A saved inbox file is not an ACK.
- If an employee is blocked, rejected, denied by policy, missing config, or unable to execute, it must reply with: current status, blocker reason, evidence path if any, and the next required action.
- If the request came from a human-facing agent, the result must return to that agent so it can notify the human operator. Required loop: human -> requesting agent -> target employee -> requesting agent -> human.
- When a service or route fails, ask whether another employee should assist and list active employee IDs as `@agent` options.
- Use `@agent` mentions to create or reuse group conversations. Participants must include the human owner/requesting agent and all mentioned active employees.
- Chat and mentions are coordination records only. Risky execution still requires an explicit task, approval, or adapter action.

## Automatic Employee Handshake

- After any agent installs or repairs this system, it must start 2-4 direct-message rounds with each other registered/reachable employee.
- On this Mac, use Hermes as the default `installer_agent` and validation admin for employee onboarding, local verification, and approval-flow verification. Codex/OpenClaw may be passed explicitly only when the user asks for that installer to own the run.
- Approval-flow verification must go through Hermes first. If a task route is blocked by approval policy, use Hermes direct read-only validation and record the blocker instead of silently skipping the verification.
- The handshake is mandatory because configuration quality depends on real replies, not guessed paths.
- Round 1 proves communication and identity: employee replies with id/name/runtime.
- Round 2 collects environment: workspace, config directory, adapter/CLI command, login/tool blockers.
- Round 3 collects routing: default reply surface, canonical runtime id/session key, whether it can report failures back to the sender.
- Round 4 is optional final validation: `ONBOARDING_READY` or `ONBOARDING_BLOCKED` with blocker and next action.
- If a round fails, keep configuring that employee until direct reply works, then rerun the failed round. If the runtime does not exist, mark `candidate` or `blocked` and include the missing command/app/path.
- Use the bundled scanner to produce or execute the handshake plan:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root /path/to/openclaw --kernel-root /path/to/company-kernel --installer-agent hermes
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root /path/to/openclaw --kernel-root /path/to/company-kernel --installer-agent hermes --handshake --handshake-rounds 3
```
- Success means every active employee completed the required rounds and returned a usable reply. Anything else is not “fully onboarded”.

## Hard Rules

- Default reply surface is the current initiating conversation until a canonical business target is locked.
- Never invent group IDs, account IDs, aliases, session keys, or customer/partner mappings.
- Candidate target/account/alias is not active until owner-confirmed and evidence-backed.
- If target, account, alias, runtime session, policy, or evidence is missing, mark blocked.
- Chat is notification and record only. Execution requires an explicit task or approved adapter action.
- Do not promote candidates or change global config automatically unless the user explicitly asks.
- Automatic discovery may create `candidate`; it must not create schedulable `active` employees without smoke evidence.
- If a different installer agent runs this skill, it still owns the same workflow: detect all supported runtimes, not only itself.

## Five-Line Employee Ask

Use this exact template when local evidence is incomplete:

```text
请按五行回复，不要解释：
1. 已能锁死的内部 alias->target：
2. 已能锁死的业务/交付 alias->target：
3. 已能锁死的 publish/partner alias->target：
4. 默认发送 account / 会话：
5. 还锁不死的 target/account 和原因：
```

Parse `无` as empty. Anything ambiguous remains `candidate` or `blocked`.

## Report Shape

Return this compact structure per employee:

```json
{
  "agent_id": "nestcar",
  "name": "car-rental",
  "status": "active|candidate|blocked",
  "identity": {
    "id": "nestcar",
    "name": "car-rental",
    "lookup_priority": ["id", "alias", "name", "display_name"],
    "rename_command": "bin/companyctl employee update --id nestcar --name <new-name>"
  },
  "runtime": {"type": "openclaw", "workspace": "/path"},
  "communication": {
    "default_reply_channel": "telegram|line|dashboard|current-conversation",
    "default_reply_account": "",
    "default_reply_target": "",
    "session_key": "",
    "direct_status": "active|candidate|blocked",
    "ack_required": true,
    "failure_feedback_required": true,
    "pending_inbox_messages": 0
  },
  "routing": {"active": [], "candidate": [], "blocked": []},
  "handshake": {
    "from": "codex",
    "to": "nestcar",
    "rounds": 3,
    "required_success": 3
  },
  "evidence": [],
  "recommended_command": "bin/companyctl ...",
  "next_action": ""
}
```

## Company Kernel Commands

Prefer API if a live dashboard is already running. Prefer CLI for deterministic setup.

```bash
bin/companyctl doctor --summary
bin/companyctl employee list
bin/companyctl employee show <agent>
bin/companyctl employee update <agent> --status active
bin/companyctl employee communication <agent> --enabled true
bin/companyctl message direct --from main --to <agent> --body "只回复：<agent>_DIRECT_OK"
bin/company-dashboard --variant advanced
```

Use `OPENCLAW_ROOT` and `OPENCLAW_COMPANY_KERNEL_ROOT` when the repo is cloned outside the default path.
