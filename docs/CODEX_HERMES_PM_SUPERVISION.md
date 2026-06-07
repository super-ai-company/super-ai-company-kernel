# Codex + Hermes PM Supervision

This document defines the portable Company Kernel pattern for using Hermes as a project manager and Codex as a developer employee.

It does not replace OpenClaw's internal communication system. OpenClaw, Hermes, Codex, Claude, Antigravity, and other tools remain runtime adapters. Company Kernel provides a separate coordination layer with durable tasks, progress evidence, and reviewable handoff records.

## Roles

- `hermes`: supervisor / project manager.
- `codex`: developer / implementation worker.
- `openclaw-main`: optional business control plane or human-facing coordinator.
- Other employees: runtime adapters that can be added after direct communication smoke passes.

## Communication Contract

The canonical durable path is:

```text
Company Kernel task
-> runtime adapter
-> workspace progress JSON
-> Hermes PM supervisor poll
-> Company Kernel task status / report
-> short human-facing event
```

Do not treat these as completion:

- inbox file exists
- direct message record exists
- `acknowledged`
- `in_progress`
- adapter dry-run report
- heartbeat only

Completion requires a matching task-scoped progress file:

```text
<codex-workspace>/reports/progress_completed_*.json
```

The progress payload must include:

```json
{
  "ok": true,
  "task_id": "<company-kernel-task-id>",
  "report": {
    "state": "completed",
    "project": "<project-name>",
    "action": "<short task summary>",
    "checking": "<verification evidence>",
    "created_at": "<iso timestamp>"
  }
}
```

Hermes ignores unrelated old progress files whose `task_id` does not match the active task.

## PM Supervisor

Run one supervision pass:

```bash
cd "$OPENCLAW_COMPANY_KERNEL_ROOT"
bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

When Hermes must supervise the active dev workspace instead of the portable runtime defaults, pass explicit roots:

```bash
bin/company-codex-pm-supervisor \
  --agent codex \
  --db-path /absolute/path/to/dev-repo/company.sqlite \
  --workspace /absolute/path/to/codex/workspace \
  --report-root /absolute/path/to/dev-repo \
  --stale-minutes 15
```

This keeps `task_id` matching, progress evidence lookup, and Hermes report output on the same dev supervision chain without mutating runtime config.

Typical outputs:

```text
完成了 Codex 的 <short task> 任务
Codex 卡住：<short task> 超过 15 分钟无完成，owner=hermes
Codex 当前没有待监督任务。
```

Detailed report files are written to:

```text
employees/hermes/reports/codex-pm/*.json
```

## Optional Scheduler Integration

Use the local scheduler of the host runtime. Do not commit machine-specific scheduler state.

Example with Hermes cron:

```bash
mkdir -p "$HERMES_HOME/scripts"
cat > "$HERMES_HOME/scripts/company_codex_pm_supervisor.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
ROOT="${OPENCLAW_COMPANY_KERNEL_ROOT:?OPENCLAW_COMPANY_KERNEL_ROOT is required}"
exec "$ROOT/bin/company-codex-pm-supervisor" --agent codex --stale-minutes "${CODEX_PM_STALE_MINUTES:-15}"
SH
chmod +x "$HERMES_HOME/scripts/company_codex_pm_supervisor.sh"

hermes cron create "every 5m" \
  --name "Company Codex PM Supervisor" \
  --script company_codex_pm_supervisor.sh \
  --no-agent \
  --deliver local \
  --workdir "$OPENCLAW_COMPANY_KERNEL_ROOT"
```

Use `--deliver local` by default. Human chat should receive only final event summaries, not monitor counters.

## OpenClaw Boundary

Company Kernel must not mutate OpenClaw's existing internal communication bus unless an adapter is explicitly invoked with execution enabled.

Portable boundary:

- Company Kernel owns portable tasks, employee registry, PM supervision, and progress evidence.
- OpenClaw owns its own native agent sessions, skills, hooks, and internal bus.
- `company-openclaw-adapter` may bridge tasks into OpenClaw only through its documented execution mode and approval gates.

## Adding Other Employees

Use the same activation sequence for Antigravity, Claude, Trae, OpenClaw business agents, or local model agents:

1. Register as `candidate`.
2. Run direct communication smoke for 2-4 rounds.
3. Require at least one progress or final evidence artifact.
4. Promote to `active` only after sender-visible receipt exists.
5. Add a runtime-specific PM supervisor only when the runtime can expose durable progress files or status APIs.

## Verification

Run:

```bash
python3 -m unittest tests.test_codex_pm_supervisor -v
bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

Pass criteria:

- completed progress with matching `task_id` closes the task;
- stale `in_progress` becomes `stalled`;
- unrelated old completed progress does not complete the current task;
- reports are written under `employees/hermes/reports/codex-pm/`.
