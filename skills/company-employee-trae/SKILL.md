---
name: company-employee-trae
description: Use when onboarding or operating Trae as a Company Kernel IDE employee, including Trae chat modes, GUI/IDE constraints, direct smoke, and evidence handoff.
---

# Company Employee: Trae

Trae is an IDE/runtime employee. Treat real execution as potentially GUI-affecting.

## One-Sentence Onboarding

“Onboard Trae as an IDE developer, verify direct reply, generate Trae chat prompts by default, and only execute in the requested mode after confirming workspace scope.”

## Required Checks

1. Verify CLI: `command -v trae` or `/usr/local/bin/trae`.
2. Register/update:
   - `bin/companyctl employee create --id trae --name Trae --role developer --runtime trae --workspace <workspace>`
3. Smoke:
   - `bin/companyctl heartbeat --agent trae`
   - `bin/companyctl message direct --from main --to trae --body "只回复：trae_DIRECT_OK"`
   - `bin/company-trae-adapter --agent trae`

## Execution Rules

- Default adapter mode is dry-run: writes a Trae chat prompt and evidence.
- `--execute` runs `trae chat --mode ask|edit|agent <prompt>`.
- Real execution may open or reuse Trae GUI state.
- Use `ask` for analysis, `edit` for scoped code edits, and `agent` only when a bounded autonomous task is acceptable.

## Blocked Cases

Block when CLI is missing, active GUI/workspace is unknown, mode is not explicit, or edit scope is not bounded.
