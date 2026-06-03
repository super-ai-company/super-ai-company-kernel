# Super AI Company / Company Kernel Delivery

## Current Status

This repository contains the local Company Kernel for the Super AI Company project.

Implemented and verified:

- SQLite-backed company kernel schema for employees, runtimes, messages, tasks, locks, heartbeats, approvals, audit logs, events, conversations, RFCs, projects, adapter runs, and task relations.
- `companyctl` command interface for employee onboarding, runtime registration, communication, task routing, task execution, approvals, recovery, heartbeats, project governance, scheduler, adapter run recovery, and doctor checks.
- Runtime adapters for OpenClaw, Hermes, Codex, Claude, Trae, and Antigravity.
- Custom runtime registration via `companyctl runtime register`, so future tools such as Cursor or Devin can be added without code changes.
- Static dashboard with runtime health, evidence health, employees, capabilities, projects, recent tasks, long-task delegation, conversations, approvals, RFCs, events, adapter runs, and locks.
- Daemon loop with heartbeat refresh, scheduler run, repair pass, compact summary output, adapter run recording, launchd template and install/uninstall scripts.
- OpenClaw alert integration in `/Users/owner/openclaw/workspace-xmanx/scripts`, including Company Kernel heartbeat, daemon, launchd, capability, and evidence health fields.

## Verification Commands

```bash
PYTHONWARNINGS=error::ResourceWarning python3 -B -m unittest discover -s tests -v
bin/company-daemon --once --summary
bin/companyctl doctor --summary
bin/company-dashboard
python3 /Users/owner/openclaw/workspace-xmanx/scripts/company_runtime_alert.py --json-only
python3 /Users/owner/openclaw/workspace-xmanx/scripts/supervisor_heartbeat.py --json-only
python3 /Users/owner/openclaw/workspace-xmanx/scripts/heartbeat_summary_router.py --print-only
```

## Latest Verified Result

- Unit tests: 22/22 passing.
- Doctor: `ok=true`, `issues=[]`.
- Heartbeats: 14 active employee heartbeats, missing=0, stale=0.
- Evidence health: 0 issues.
- Capability health: 0 issues.
- OpenClaw runtime alert: `severity=ok`.

## Runtime Note

The launchd service is prepared but not installed in this delivery because installing it requires macOS launchctl/user-agent changes. Manual verification uses `bin/company-daemon --once --summary`.

## Repository Scope

Committed:

- Source code under `company_kernel/`
- Executable wrappers under `bin/`
- Config templates under `config/`
- Docs and tests
- Employee profile/capability/permission/rule files

Ignored:

- SQLite runtime database
- Logs
- Generated dashboard and daemon state
- Employee inbox/outbox/report runtime artifacts
- Python caches
