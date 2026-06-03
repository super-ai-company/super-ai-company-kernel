---
name: company-employee-codex
description: Use when onboarding or operating Codex as a Company Kernel employee, including direct message smoke, task card generation, sandbox selection, evidence reports, and safe code execution.
---

# Company Employee: Codex

Codex is a developer employee. It should work from a canonical repo, produce evidence, and only execute with explicit scope.

## One-Sentence Onboarding

“Onboard Codex for this repo, verify direct reply, generate task cards by default, and only execute code with an explicit sandbox and workspace.”

## Required Checks

1. Verify CLI: `command -v codex`.
2. Resolve workspace:
   - `OPENCLAW_CODEX_WORKSPACE` or explicit project path.
   - Never assume a business workspace from a chat name.
3. Register/update:
   - `bin/companyctl employee create --id codex --name Codex --role developer --runtime codex --workspace <repo>`
4. Smoke:
   - `bin/companyctl heartbeat --agent codex`
   - `bin/companyctl message direct --from main --to codex --body "只回复：codex_DIRECT_OK"`
   - `bin/company-codex-adapter --agent codex`

## Execution Rules

- Default adapter mode is dry-run: creates `codex-task-card.md` and report.
- `--execute` runs `codex exec`.
- Default sandbox should stay read-only unless task explicitly requires writes.
- For code edits, pass `--sandbox workspace-write` and keep edits inside the canonical workspace.
- On success, complete the task with changed files and verification. On failure, block with report path.

## Blocked Cases

Block when workspace is missing, repo scope is unclear, task asks for secrets/external sends/payments/destructive DB writes, or required sandbox is not approved.
