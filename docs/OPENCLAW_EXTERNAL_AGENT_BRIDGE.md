# OpenClaw External Agent Bridge

This bridge lets OpenClaw delegate tasks to Codex and Antigravity/Agy without
making OpenClaw directly run unstable CLI commands.

## Purpose

OpenClaw remains the business task entrypoint. Company Kernel manages the
external runtime lifecycle, task ledger, evidence, and dashboard visibility.

Flow:

```text
OpenClaw agent_bus/inbox/codex|antigravity|agy
  -> company-openclaw-external-bridge
  -> Company Kernel task
  -> Codex/Agy adapter or bridge dry-run
  -> Company Kernel done/blocked with evidence
  -> OpenClaw agent_bus/done|failed/<agent>
```

## Safety Defaults

- Default does not process real OpenClaw files unless `--execute` is passed.
- Default does not start Codex or Agy unless `--execute-adapter` is passed.
- Without `--execute-adapter`, the bridge creates Kernel task/evidence and
  closes the OpenClaw item as a safe dry-run.
- The bridge does not modify OpenClaw router, worker, supervisor, Telegram, or
  approval watcher code.

## Task Shape

Write an OpenClaw bus file under:

```text
/Users/shift/openclaw/ops/agent_bus/inbox/codex/<task_id>.json
/Users/shift/openclaw/ops/agent_bus/inbox/antigravity/<task_id>.json
/Users/shift/openclaw/ops/agent_bus/inbox/agy/<task_id>.json
```

Example:

```json
{
  "task_id": "oc-codex-readme-001",
  "created_at": "2026-06-11T00:00:00",
  "source_agent": "main",
  "target_agent": "codex",
  "type": "external_agent_task",
  "priority": "P2",
  "payload": {
    "instruction": "Read README and summarize current capabilities.",
    "skill_id": "repo-inspection"
  },
  "status": "submitted"
}
```

## Commands

Dry-run inspect only:

```bash
bin/company-openclaw-external-bridge --agents codex,antigravity,agy --limit 5
```

Safe bridge execution without starting Codex/Agy:

```bash
bin/company-openclaw-external-bridge --agents codex,antigravity,agy --limit 5 --execute
```

Real adapter execution:

```bash
bin/company-openclaw-external-bridge --agents codex --limit 1 --execute --execute-adapter --adapter-timeout 600
```

Use real adapter execution only after the safe bridge mode is green.

## State Mapping

- Adapter success -> Company Kernel task `completed` -> OpenClaw `done`.
- Adapter blocked/failed -> Company Kernel task `blocked` -> OpenClaw `failed`.
- Evidence is written under Company Kernel task workspace or
  `reports/openclaw-external-agent-bridge/`.

## Current Status

Implemented as a manual worker entrypoint. It is not enabled in launchd or the
Company Kernel daemon by default.
