---
name: company-employee-hermes
description: Use when onboarding or operating Hermes as a Company Kernel supervisor employee, including oneshot prompts, direct smoke, model/tool constraints, and evidence reporting.
---

# Company Employee: Hermes

Hermes is a supervisor/automation employee. It may coordinate and report, but must not bypass approval or mutate runtime/provider configuration without explicit request.

## One-Sentence Onboarding

“Onboard Hermes as a supervisor, verify direct reply, generate a Hermes oneshot prompt by default, and keep provider/proxy/tool changes blocked unless explicitly approved.”

## Required Checks

1. Locate Hermes:
   - `command -v hermes`, `/Users/shift/.local/bin/hermes`, `/Users/shift/.hermes`, or `/Users/shift/hermes`.
2. Register/update:
   - `bin/companyctl employee create --id hermes --name Hermes --role supervisor --runtime hermes --workspace <hermes-home>`
3. Smoke:
   - `bin/companyctl heartbeat --agent hermes`
   - `bin/companyctl message direct --from main --to hermes --body "只回复：hermes_DIRECT_OK"`
   - `bin/company-hermes-adapter --agent hermes`

## Execution Rules

- Default adapter mode is dry-run: writes a `hermes -z` oneshot prompt and evidence.
- `--execute` may run `hermes -z <prompt>`.
- Do not change remote proxy, containers, model providers, auth, or tool config unless the task explicitly asks and approval is clear.
- Hermes can supervise routing and review evidence, but task execution must still go through Company Kernel status/evidence.

## Blocked Cases

Block when Hermes CLI is missing, provider/auth status is unknown, requested action changes infrastructure config without approval, or no evidence path can be produced.
