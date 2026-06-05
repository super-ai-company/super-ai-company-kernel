# Internal Communication Closure Report

- generated_at: `2026-06-05 14:39:50 +07`
- repo: `/Users/shift/openclaw/workspace-xmanx/projects/super-ai-company-kernel`
- scope: dev repo only; no active runtime deploy/restart

## Problem

Agent-to-agent communication could look successful because a message or inbox file existed, while the receiving agent did not actually claim, execute, block, or return evidence. Human chat responsiveness was also not proof that internal agent queues were being processed.

## Implemented Closure

1. Direct/private message observability:
   - `/v1/messages/recent-direct`
   - dashboard Communication Observatory direct feed

2. External mirror observability:
   - sanitized external thread/message import
   - idempotent external message import
   - cursor/upsert visibility

3. Adapter run summary:
   - repo-local progress JSON only
   - dashboard Adapter Run Summary panel

4. Internal watchdog:
   - `/v1/dashboard/internal-watchdog`
   - detects `no_receipt_messages`
   - detects `open_tasks` in submitted/claimed state

5. Remediation:
   - `/v1/dashboard/internal-watchdog/remediate`
   - dry-run planning
   - first follow-up creation
   - escalation to Hermes/main when follow-up already exists
   - reroute decision envelope generation

6. Reroute execution:
   - `/v1/dashboard/internal-watchdog/apply-reroutes`
   - reads answered `reroute-*` followups
   - creates `rerouted-<original_task_id>` for new owner
   - blocks original stalled task with reroute evidence

7. Skills/runbooks:
   - `skills/company-employee-cli-hermes/SKILL.md`
   - `skills/company-employee-openclaw-workspace/SKILL.md`
   - `skills/company-employee-codex/SKILL.md`
   - `skills/company-employee-antigravity/SKILL.md`
   - `skills/openclaw-local-agent-bootstrap/SKILL.md`
   - `README.md` Internal Communication Watchdog Runbook

## Commit Staging Advice

Recommended include for the communication-closure commit:

- `README.md`
- `company_kernel/api_gateway.py`
- `company_kernel/company_dashboard.py`
- `company_kernel/openclaw_adapter.py`
- `tests/test_company_kernel_core.py`
- `skills/company-employee-cli-hermes/SKILL.md`
- `skills/company-employee-openclaw-workspace/SKILL.md`
- `skills/company-employee-codex/SKILL.md`
- `skills/company-employee-antigravity/SKILL.md`
- `skills/openclaw-local-agent-bootstrap/SKILL.md`
- `reports/internal-communication-closure-20260605.md`

Candidate-but-review-first files because they may include adjacent work outside the strict watchdog closure:

- `company_kernel/companyctl.py`
- `company_kernel/schema.sql`
- `config/company_communications.json`
- `dashboard_templates/gemini_dashboard.html`
- `employees/*/profile.json`
- `design-system/`
- `rfcs/20260605-direct-telegram-backend-closure.md`
- `scripts/`

Ignore/keep local only:

- `.ops/`
- `reports/heartbeats/`
- `reports/*.log`
- `reports/tmp-chrome-profile/`

## Verification

```bash
/Users/shift/hermes-upgrades/hermes-agent-latest/venv/bin/python -m pytest tests/test_company_kernel_core.py -q
```

Result: `98 passed in 16.89s` at last full run before this report section was written.

## Postmortem Rule

No internal message counts as handled until the sender receives a structured receipt. No execution request counts as complete until it has evidence and verification. If follow-up does not resolve the stall, escalate to Hermes/main and require a reroute decision: `continue_original | reroute | block | ask_human`.
