---
name: openclaw-local-agent-bootstrap
description: Use when installing or configuring OpenClaw on a customer machine to discover local workspaces, build AI employee models, lock communication surfaces, classify active/candidate/blocked routing, and produce a safe onboarding report before enabling employees.
---

# OpenClaw Local Agent Bootstrap

Use this skill before onboarding or repairing OpenClaw employees in a new local environment. Do not write config first. Discover, classify, ask only for missing facts, then verify.

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

For a read-only first pass, run the bundled scanner:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root /path/to/openclaw --kernel-root /path/to/company-kernel
```

Use its output as a draft only. It cannot promote candidates or verify direct communication by itself.

## Hard Rules

- Default reply surface is the current initiating conversation until a canonical business target is locked.
- Never invent group IDs, account IDs, aliases, session keys, or customer/partner mappings.
- Candidate target/account/alias is not active until owner-confirmed and evidence-backed.
- If target, account, alias, runtime session, policy, or evidence is missing, mark blocked.
- Chat is notification and record only. Execution requires an explicit task or approved adapter action.
- Do not promote candidates or change global config automatically unless the user explicitly asks.

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
  "status": "active|candidate|blocked",
  "runtime": {"type": "openclaw", "workspace": "/path"},
  "communication": {
    "default_reply_channel": "telegram|line|dashboard|current-conversation",
    "default_reply_account": "",
    "default_reply_target": "",
    "session_key": "",
    "direct_status": "active|candidate|blocked"
  },
  "routing": {"active": [], "candidate": [], "blocked": []},
  "evidence": [],
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
