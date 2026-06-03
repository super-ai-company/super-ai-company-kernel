---
name: company-employee-gui-antigravity
description: Use when onboarding or operating Google Antigravity as a Company Kernel GUI candidate/employee, including app launch, GUI task briefs, manual completion, and evidence/blocker reporting. GUI brief ACK is not enough for active status.
---

# Company Employee: Antigravity

Antigravity is a GUI employee. There may be no stable CLI, so keep execution explicit and evidence-driven.

## One-Sentence Onboarding

“Onboard Antigravity as a GUI developer, verify direct reply, generate GUI task briefs by default, and require manual evidence or blocker for completion.”

## Required Checks

1. Locate app:
   - `/Applications/Antigravity.app`
   - Bundle id: `com.google.antigravity`
2. Register/update as candidate, not active:
   - `bin/companyctl employee create --id antigravity --name Antigravity --role developer --runtime antigravity --workspace <workspace>`
3. Smoke and activation:
   - `bin/companyctl heartbeat --agent antigravity`
   - `bin/companyctl message direct --from main --to antigravity --body "只回复：antigravity_DIRECT_OK"`
   - `bin/companyctl employee verify-direct --id antigravity --from main --rounds 3 --activate`
   - `bin/company-antigravity-adapter --agent antigravity`

## Execution Rules

- Default adapter mode is dry-run: writes a GUI task brief and evidence.
- Direct messages must not fail with “unsupported runtime”. They must write a direct GUI brief under `employees/antigravity/reports/direct/`, return an ACK to the sender, and clearly state that implementation still requires GUI evidence or a blocker.
- Direct GUI brief ACK keeps Antigravity as `candidate`; it does not prove active employee readiness.
- Active status is forbidden until `employee verify-direct` completes 2-4 rounds with receipt and the runtime has a real implementation/blocker evidence return path.
- `--execute` only opens Antigravity; it does not prove task completion.
- Use `--complete --task-id <id> --summary ... --evidence ...` or equivalent Company Kernel task completion after GUI work has real evidence.
- Use `--block --task-id <id> --blocker ...` if the GUI cannot complete safely.
- Every received request must ACK or return a blocker reply to the sender.
- If asked to optimize UI, Antigravity must inspect every dashboard page in the browser before proposing or implementing changes: Overview, Tasks & Workflows, Projects & Plans, AI Employees, Governance, Logs & Events, Trace Telemetry.
- If Antigravity cannot operate the GUI or commit code, it must return a blocker to the requesting agent and suggest `@codex` or another active employee to implement the changes.
- On failure, include status, blocker, evidence path, and next action; suggest active `@agent` collaborators when helpful.
- Human-originated requests must return through the requesting agent so the human operator receives a clear update.

## Blocked Cases

Block when app is missing, GUI state cannot be verified, task requires hidden credentials, or no evidence can be attached.
