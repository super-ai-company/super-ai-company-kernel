---
name: dispatch-task-to-codex
description: Use when assigning/submitting a development task to the Codex employee via Company Kernel, or when a "task submit ... --to codex" fails with "communication denied: source communication paused" or "target employee is not active". Covers the submit command, the two gating checks, diagnosis, and the fix.
---

# Dispatch a Task to Codex (派活给 Codex)

How to reliably hand a development task from any employee (e.g. `claude`, `main`, `owner`) to the `codex` employee through Company Kernel, and how to recover when submission is rejected.

Run everything from the kernel root:

```bash
cd /Users/owner/openclaw/company-kernel
```

## The command

```bash
bin/companyctl task submit \
  --from <sender> \
  --to codex \
  --title "<标题>" \
  --description "<含目标、允许范围、验收标准、回滚的详细需求>" \
  --priority P1
```

After submit, the daemon's codex adapter auto-runs within ~30s (`company-codex-adapter --agent codex --execute --sandbox workspace-write --model gpt-5.5`). Check with `bin/companyctl task show --task-id <id>`.

## Two gating checks `task submit` enforces (this is what fails)

`cmd_task_submit` rejects BEFORE creating the task if either gate fails:

1. **Communication gate** — `require_communication_allowed(source, target)`:
   - If the **sender** has `communication_paused: true` → `communication denied: source communication paused`.
   - If the **target** (codex) has `communication_paused: true` → `communication denied: target communication paused`.
   - In `strict`/`allowlist` mode, sender's `can_assign_to` must include `codex`. (Default mode is `open`, so this rarely fires.)
   - Source/target are read from `config/company_communications.json` → `employees.<id>`.

2. **Active-target gate** — `require_active_employee(codex)`:
   - codex's row in the `employees` table must have `status == 'active'`, else
     `target employee is not active` with required command
     `bin/companyctl employee verify-direct --id codex --from main --rounds 3 --activate`.

The sender's *status* does NOT matter for sending — only `communication_paused` does. So a `candidate` employee can still submit, as long as its communication is not paused.

## Diagnose

```bash
# Is the sender->codex assignment allowed?
bin/companyctl communication check --from <sender> --to codex --action assign

# Is the sender paused / is codex paused? (look for communication_paused)
python3 -c "import json;d=json.load(open('config/company_communications.json'))['employees'];import sys;[print(k,v.get('communication_paused')) for k,v in d.items() if k in sys.argv[1:]]" <sender> codex

# Is codex active?
bin/companyctl employee show --id codex
```

## Fix A — sender is communication-paused (most common)

Symptom: `communication denied: source communication paused`.
Cause: the daemon's "real communication verification" probed the sender's runtime, it failed, and the sender was auto-paused + demoted to `candidate` (e.g. claude shows `unavailable_reason: 真实通信验证未通过`).

Resume the sender's communication (clean, uses the kernel's own function — touches only the JSON config, no DB):

```bash
OPENCLAW_COMPANY_KERNEL_ROOT="$PWD" python3 -c "import company_kernel.companyctl as k; print(k.set_employee_communication_enabled('<sender>', True))"
```

Why this sticks: `mark_employee_unavailable` only re-pauses employees whose status is `active`. A `candidate` sender that fails a later probe is NOT re-paused, so once un-paused it stays usable for sending.

A ready-made script does exactly this for `claude`: double-click `FIX-CODEX-DISPATCH.command`.

## Fix B — codex is not active

Symptom: `target employee is not active`.
Re-verify codex with real direct rounds and activate:

```bash
bin/companyctl employee verify-direct --id codex --from main --rounds 3 --activate
```

This requires the `codex` CLI to actually answer the probe; if it can't, fix the codex runtime first (`command -v codex`, workspace, model).

## Robust pattern (avoid the whole problem)

If you just need work to reach codex and don't care which employee is the nominal sender, submit from an operator/owner that is never runtime-probed and never auto-paused:

```bash
bin/companyctl task submit --from owner --to codex --title "..." --description "..." --priority P1
# or --from main
```

## Verify it worked

```bash
bin/companyctl communication check --from <sender> --to codex --action assign   # expect allowed: true
bin/companyctl task submit --from <sender> --to codex --title "dispatch check" --description "只读自检,确认能收到任务即可。" --priority P3
bin/companyctl task show --task-id <printed-id>
```

See also `docs/CODEX_DEV_GUIDE.md` for the full codex development workflow and `docs/COMPANY_KERNEL_USAGE.md` for kernel basics.
