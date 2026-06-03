---
name: company-employee-hermes
description: Use when onboarding or operating Hermes as a Company Kernel supervisor employee, including oneshot prompts, direct smoke, model/tool constraints, and evidence reporting.
---

# Company Employee: Hermes

Hermes is a supervisor/automation employee. It may coordinate and report, but must not bypass approval or mutate runtime/provider configuration without explicit request.

## One-Sentence Onboarding

“Onboard Hermes as a supervisor, verify direct reply, generate a Hermes oneshot prompt by default, and keep provider/proxy/tool changes blocked unless explicitly approved.”

## Installer Responsibility

If Hermes is the agent installing Company Kernel/OpenClaw, do not only configure Hermes. Run the bootstrap scanner first, discover Codex/OpenClaw/Claude/Trae/Antigravity/local candidates, and only create them as `candidate` until direct smoke passes.

## Required Checks

1. Locate Hermes:
   - `command -v hermes`, `/Users/shift/.local/bin/hermes`, `/Users/shift/.hermes`, or `/Users/shift/hermes`.
   - If Hermes is reached through OpenClaw runtime, discover its canonical runtime agent id before sending. The Company Kernel employee id may be `hermes` while the runtime agent id is `default`.
2. Register/update:
   - `bin/companyctl employee create --id hermes --name Hermes --role supervisor --runtime hermes --workspace <hermes-home>`
3. Smoke:
   - `bin/companyctl heartbeat --agent hermes`
   - `bin/companyctl message direct --from main --to hermes --body "只回复：hermes_DIRECT_OK"`
   - `bin/company-hermes-adapter --agent hermes`

## Runtime Identity Rule

- Keep Company Kernel employee id as `hermes` unless the owner asks otherwise.
- Do not assume the runtime agent id is also `hermes`.
- If the local Hermes/OpenClaw runtime canonical id is `default`, direct runtime calls must use:
  - `--agent default`
  - `session_key=agent:default:<source>`
- They must not use:
  - `--agent hermes`
  - `session_key=agent:hermes:<source>`
- Agent-to-agent allowlists in the runtime must include the runtime id such as `default`, not only the Company Kernel employee id.
- Main relay is only a blocker/OPS fallback. Do not make it the default communication path when a direct reply surface is confirmed.

## Execution Rules

- Default adapter mode is dry-run: writes a `hermes -z` oneshot prompt and evidence.
- `--execute` may run `hermes -z <prompt>`.
- Do not change remote proxy, containers, model providers, auth, or tool config unless the task explicitly asks and approval is clear.
- Hermes can supervise routing and review evidence, but task execution must still go through Company Kernel status/evidence.
- Prefer SQLite/local state as the memory source of truth unless the owner explicitly confirms a different canonical memory backend.

## Blocked Cases

Block when Hermes CLI is missing, provider/auth status is unknown, runtime agent id and session key disagree, runtime agent-to-agent allowlist denies the target, requested action changes infrastructure config without approval, or no evidence path can be produced.
