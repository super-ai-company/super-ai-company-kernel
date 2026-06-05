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
- Execution-class direct messages must not stop at receipt. The adapter must write repo-local progress files and send a status message back to the source.
- Required states are `acknowledged`, `in_progress`, `blocked`, and `completed`. `acknowledged` is not work evidence.
- A valid execution smoke must produce both:
  - source inbox message containing `status: working`;
  - final receipt containing `status: done` or `status: blocked`.
- Codex must reply at least once to every received employee request. If blocked or rejected, reply to the sender with status, blocker, evidence path, and the next action.
- If another employee is needed, name active collaborators as `@agent` options and ask the sender whether to add them.
- For human-facing requests, close the loop back to the requesting agent so it can notify the human operator; do not leave the human waiting on an internal inbox record.

## Postmortem: 2026-06-04 Why Codex Was Not Continuously Controllable

Observed failure:

- Company Kernel could create Codex inbox records and task cards, but this was sometimes reported like Codex was actually working.
- Some requests stopped after `acknowledged`; the human-facing loop did not always receive `in_progress` and final `done|blocked` receipts.
- A dry-run or policy-gated task was confused with implementation progress.
- Codex attempted to use a missing `scripts/progress_report.py` helper in one flow, so there was no standardized progress artifact for the supervisor to read.
- Kernel-change tasks could be blocked by approval policy. That was correct safety behavior, but without a structured blocker reply it looked like Codex had gone silent.

Root causes:

1. `message send` was record-only; it did not invoke `company-codex-adapter --direct-message`.
2. Heartbeat/employee existence was treated as liveness/control evidence.
3. The adapter did not always enforce a two-phase progress receipt (`working` then `done|blocked`).
4. Progress evidence location was not standardized enough for dashboard/Hermes summaries.
5. The requesting agent did not always close the result back to the human.

Preventive rules for future installs/customers:

- Use `message direct` for control probes; never accept inbox files as proof.
- Keep Codex `candidate` until 2-4 direct rounds pass and a sender-visible receipt exists for every round.
- Execution tasks must write repo-local progress JSON under `reports/` and send a final receipt.
- If approval/policy blocks execution, Codex must return `status: blocked`, the policy reason, evidence path, and the next required action.
- Hermes/main must verify changed files, tests, and progress artifacts before telling the human Codex completed work.

## Verified Direct Execution Loop

Use this smoke after onboarding or repair:

```bash
bin/companyctl message direct \
  --from main \
  --to codex \
  --body "执行一次只读闭环烟测：不要修改业务代码，只检查当前仓库状态并按要求返回 status/current_action/changed_files/verification_run/blocker/eta。" \
  --timeout 120
bin/companyctl message list --agent main
```

Pass criteria:

- `main -> codex` request is recorded.
- `codex -> main` progress message exists with `status: working`.
- `codex -> main` receipt exists with `status: done` or `status: blocked`.
- Codex workspace contains `reports/progress_acknowledged_*.json`, `reports/progress_in_progress_*.json`, and final `progress_completed_*.json` or `progress_blocked_*.json`.
- The final reply includes concrete `changed_files`, `verification_run`, `blocker`, and `eta`; echoing the prompt is failure.

## Main/Codex Role Boundary

- Main/OpenClaw owns project registration, scope, acceptance, sequencing, GitHub finalization, and human-facing status.
- Codex owns bounded implementation/review inside the assigned repo.
- Main must verify `git status`, `git diff --stat`, changed files, tests, runtime/browser evidence, and artifacts before accepting Codex output.
- Codex must not redefine the product goal, touch unrelated business workspaces, or turn pending/candidate states into success.

## Execution Rules

- Default adapter mode is dry-run: creates `codex-task-card.md` and report.
- `--execute` runs `codex exec`.
- Default sandbox should stay read-only unless task explicitly requires writes.
- For code edits, pass `--sandbox workspace-write` and keep edits inside the canonical workspace.
- On success, complete the task with changed files and verification. On failure, block with report path.

## Blocked Cases

Block when workspace is missing, repo scope is unclear, task asks for secrets/external sends/payments/destructive DB writes, or required sandbox is not approved.
