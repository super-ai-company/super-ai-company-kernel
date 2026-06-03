---
name: company-employee-claude
description: Use when onboarding or operating Claude as a Company Kernel analyst employee, including Claude CLI print prompts, permission mode handling, direct smoke, and evidence reports.
---

# Company Employee: Claude

Claude is an analyst/review employee. Prefer bounded analysis, docs, and review tasks.

## One-Sentence Onboarding

“Onboard Claude as an analyst, verify direct reply, generate Claude print prompts by default, and require explicit permission mode for elevated actions.”

## Required Checks

1. Verify CLI: `command -v claude`.
2. Register/update:
   - `bin/companyctl employee create --id claude --name Claude --role analyst --runtime claude --workspace <workspace>`
3. Smoke:
   - `bin/companyctl heartbeat --agent claude`
   - `bin/companyctl message direct --from main --to claude --body "只回复：claude_DIRECT_OK"`
   - `bin/company-claude-adapter --agent claude`

## Execution Rules

- Default adapter mode is dry-run: writes a `claude -p` prompt and evidence.
- `--execute` runs `claude -p <prompt> --no-session-persistence --output-format text`.
- Default permission mode is `default`; require explicit `--permission-mode` for anything else.
- Use Claude for analysis, docs, review, code understanding, and second opinions; code-changing tasks need scoped workspace and evidence.
- Every employee request must get at least one ACK or blocker reply to the sender.
- On failure, return status, blocker, evidence path, and next action; if collaboration helps, suggest active `@agent` options.
- Human-facing requests must be routed back to the requesting agent so the human receives the result.

## Blocked Cases

Block when CLI/auth is missing, permission mode is unclear, workspace scope is ambiguous, or task requires external/high-risk actions without approval.
