---
name: company-employee-openclaw
description: Use when onboarding or operating an OpenClaw runtime employee such as main, nestcar, chindahotpot, invest, krothong, video-creator, video-ops, or video-publisher inside Company Kernel.
---

# Company Employee: OpenClaw

OpenClaw employees bridge Company Kernel tasks into OpenClaw workspaces and legacy bus. Keep discovery and routing strict.

## One-Sentence Onboarding

“Onboard this workspace as an OpenClaw employee, verify direct reply, keep external sends approval-gated, and mark unresolved targets as blocked.”

## Installer Responsibility

If OpenClaw main installs or repairs the Company Kernel, it must discover all local employees, not only OpenClaw workspaces. Run:

```bash
python3 skills/openclaw-local-agent-bootstrap/scripts/scan_install.py --openclaw-root <openclaw-root> --kernel-root <company-kernel-root>
```

Use `--apply` only to create discovered employees as `candidate`. Direct smoke and owner-confirmed routing are required before active promotion.

## Required Checks

1. Locate OpenClaw:
   - `OPENCLAW_ROOT` or `/Users/shift/openclaw`.
   - Must contain `scripts/oc`.
2. Locate employee workspace:
   - `workspace-<agent>` or explicit user path.
   - Read `AGENTS.md`, state files, local routing docs, and config if present.
3. Register or update:
   - `bin/companyctl employee create --id <agent> --name <name> --role business-agent --runtime openclaw --workspace <path>`
   - Then set capabilities and permissions if known.
4. Verify:
   - `bin/companyctl heartbeat --agent <agent>`
   - `bin/companyctl message direct --from main --to <agent> --body "只回复：<agent>_DIRECT_OK"`
   - `bin/company-openclaw-adapter --agent <agent>` for dry-run task bridge.

## Execution Rules

- Dry-run adapter writes `employees/<agent>/reports/<task-id>/openclaw-adapter-report.md`.
- `--execute` submits to OpenClaw bus and requires `external_send` approval.
- Supported legacy bus employees include `main`, `nestcar`, `chindahotpot`, `invest`, `video-creator`, `video-publisher`, `video-ops`, `krothong`.
- Do not write directly to business inboxes. Use the adapter and approval gates.
- If default reply target/account/alias is missing, reply only to current initiating conversation.

## Blocked Cases

Block when `scripts/oc` is missing, workspace is ambiguous, bus target is unsupported, default reply target is unverified, or alias/account has no evidence.
