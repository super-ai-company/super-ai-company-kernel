---
name: company-employee-antigravity
description: Use when onboarding or operating Google Antigravity as a Company Kernel GUI employee, including app launch, GUI task briefs, manual completion, and evidence/blocker reporting.
---

# Company Employee: Antigravity

Antigravity is a GUI employee. There may be no stable CLI, so keep execution explicit and evidence-driven.

## One-Sentence Onboarding

“Onboard Antigravity as a GUI developer, verify direct reply, generate GUI task briefs by default, and require manual evidence or blocker for completion.”

## Required Checks

1. Locate app:
   - `/Applications/Antigravity.app`
   - Bundle id: `com.google.antigravity`
2. Register/update:
   - `bin/companyctl employee create --id antigravity --name Antigravity --role developer --runtime antigravity --workspace <workspace>`
3. Smoke:
   - `bin/companyctl heartbeat --agent antigravity`
   - `bin/companyctl message direct --from main --to antigravity --body "只回复：antigravity_DIRECT_OK"`
   - `bin/company-antigravity-adapter --agent antigravity`

## Execution Rules

- Default adapter mode is dry-run: writes a GUI task brief and evidence.
- `--execute` only opens Antigravity; it does not prove task completion.
- Use `--complete --task-id <id> --summary ... --evidence ...` or equivalent Company Kernel task completion after GUI work has real evidence.
- Use `--block --task-id <id> --blocker ...` if the GUI cannot complete safely.

## Blocked Cases

Block when app is missing, GUI state cannot be verified, task requires hidden credentials, or no evidence can be attached.
