---
name: company-employee-codex
description: Use when onboarding or operating Codex as a Company Kernel employee, including direct message smoke, task card generation, sandbox selection, evidence reports, and safe code execution.
---

# Company Employee: Codex

Codex is a developer employee. It should work from a canonical repo, produce evidence, and only execute with explicit scope.

## One-Sentence Onboarding

“Onboard Codex for this repo, verify direct reply, generate task cards by default, and only execute code with an explicit sandbox and workspace.”

## Installer Responsibility

If Codex is the agent installing Company Kernel/OpenClaw, do not only configure Codex. First run the bootstrap scanner and discover all supported local employees:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root <openclaw-root> --kernel-root <company-kernel-root>
```

If the user asks for automatic setup, rerun with `--apply` to create missing employees as `candidate`, then smoke each one before promotion.
After scan/apply, Codex must run or output the 2-4 round handshake plan as the installer agent. Use `--installer-agent codex --handshake-rounds 3`; use `--handshake` when direct execution is allowed. Codex is not done until reachable employees have replied or are explicitly marked missing/blocked.

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

## Message Semantics

- Inbox messages to `employees/codex/inbox/*.message.json` are records, not proof that Codex has read or answered.
- For a required ACK, call `bin/companyctl message direct --from <source> --to codex --body "只回复：CODEX_ACK"` or use `POST /v1/messages/direct`.
- The Codex direct path runs `company-codex-adapter --direct-message` and returns an immediate adapter reply.
- If OpenClaw sends with record-only `message send`, the expected state is pending inbox until a daemon/adapter/human explicitly processes it.
- Codex must reply at least once to every received employee request. If blocked or rejected, reply to the sender with status, blocker, evidence path, and the next action.
- If another employee is needed, name active collaborators as `@agent` options and ask the sender whether to add them.
- For human-facing requests, close the loop back to the requesting agent so it can notify the human operator; do not leave the human waiting on an internal inbox record.

## Execution Rules

- Default adapter mode is dry-run: creates `codex-task-card.md` and report.
- `--execute` runs `codex exec`.
- Default sandbox should stay read-only unless task explicitly requires writes.
- For code edits, pass `--sandbox workspace-write` and keep edits inside the canonical workspace.
- On success, complete the task with changed files and verification. On failure, block with report path.

## Blocked Cases

Block when workspace is missing, repo scope is unclear, task asks for secrets/external sends/payments/destructive DB writes, or required sandbox is not approved.
