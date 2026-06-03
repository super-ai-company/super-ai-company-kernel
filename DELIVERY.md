# Super AI Company / Company Kernel Delivery

## Current Status

This repository contains the local Company Kernel for the Super AI Company project.

Implemented and verified:

- SQLite-backed company kernel schema for employees, runtimes, messages, tasks, locks, heartbeats, approvals, audit logs, events, conversations, RFCs, projects, adapter runs, and task relations.
- `companyctl` command interface for employee onboarding, runtime registration, communication, task routing, task execution, approvals, recovery, heartbeats, project governance, scheduler, adapter run recovery, and doctor checks.
- Employee onboarding can scaffold managed workspaces with runtime-specific rules, and offboarding supports dry-run, soft archive, and guarded hard delete.
- Runtime adapters for OpenClaw, Hermes, Codex, Claude, Trae, and Antigravity.
- Antigravity GUI adapter can now return completed or blocked GUI results back into Company Kernel with evidence/blocker.
- OpenClaw adapter bridge tests cover dry-run payload/evidence generation and `--execute` approval gating before legacy bus submit.
- Codex adapter tests cover task-card/evidence generation, mocked `codex exec` success completion, and failed execution blocking with report.
- CLI-backed adapters compact runtime stdout/stderr into task summaries/blockers while keeping full output as evidence files for low-token monitoring.
- Custom runtime registration via `companyctl runtime register`, so future tools such as Cursor or Devin can be added without code changes.
- End-to-end daemon worker smoke for automatic task execution: daemon can enable a worker, claim a task, write evidence, complete it, heartbeat, and record `adapter_runs`.
- Trace ID telemetry foundation: task metadata, company events, adapter runs, and dashboard now carry the same trace id.
- Trace telemetry export: `bin/company-trace` writes per-trace JSON and HTML timeline files for dispatch, hook, and adapter latency inspection.
- Retry policy foundation: daemon records adapter attempts and `next_retry_at`, then automatically restores due failed adapter tasks through the existing recovery path.
- API Gateway foundation: lightweight REST service exposes health, doctor, tasks, messages, heartbeats, and adapter runs while reusing `companyctl` governance.
- API Gateway collaboration and governance endpoints now expose conversations, approvals, adapter-run detail, acknowledgement, and retry without direct SQLite access.
- API Gateway service discovery and OpenAPI descriptors expose machine-readable capabilities, endpoints, and governance constraints for multi-machine employees.
- API Gateway task execution endpoints now let remote employees claim, complete with evidence, or block tasks through the same `companyctl` rules.
- API Gateway task recovery endpoints now let supervisors reopen and reassign interrupted work without direct SQLite access.
- API Gateway project governance endpoints now expose project creation, listing, task linking, plan item updates, and project status changes.
- API Gateway project review and acceptance endpoints now expose readiness checks and completion acceptance records.
- API Gateway lock endpoints now expose acquire/list/release/unlock-stale for distributed worker coordination.
- API Gateway employee and runtime endpoints now expose employee creation, employee inspection, runtime registration, and runtime listing.
- API Gateway employee governance endpoints now expose capability and permission updates.
- API Gateway employee lifecycle endpoints now expose onboard/offboard without direct SQLite access.
- API Gateway routing endpoints now expose employee capability matching and governed automatic task routing through REST.
- RPC Gateway now exposes the same governed service layer through JSON-RPC plus a generic gRPC server contract.
- Optional `requirements-optional.txt` pins `grpcio` for deployments that need real gRPC network service validation.
- Service smoke now starts REST/RPC on random local ports and validates remote health/describe/get without direct SQLite access; with optional dependencies installed it also reports gRPC `ready`.
- Owner `owner` is registered as a human approval endpoint. Telegram button approval was verified through `/Users/owner/openclaw/scripts/ops_telegram_approval_watcher.py`, but that direct watcher is disabled by default because it conflicts with OpenClaw's own Telegram `getUpdates` loop; production should use an OpenClaw-native callback/plugin bridge.
- Tools that are not installed or connected on this Mac, such as Cursor, Devin, GitHub Copilot, and local-model-agent, are tracked as `candidate` employees instead of active employees; they are excluded from heartbeat and routing until activated.
- Sandbox isolation foundation: Codex/Hermes adapters can wrap execution commands with Docker or Firejail profiles without changing task protocol.
- Static dashboard with runtime health, evidence health, employees, capabilities, projects, recent tasks, long-task delegation, conversations, approvals, RFCs, events, adapter runs, and locks.
- Daemon loop with heartbeat refresh, scheduler run, repair pass, compact summary output, adapter run recording, launchd template and install/uninstall scripts.
- OpenClaw alert integration in `/Users/owner/openclaw/workspace-xmanx/scripts`, including Company Kernel heartbeat, daemon, launchd, capability, and evidence health fields.

## Verification Commands

```bash
PYTHONWARNINGS=error::ResourceWarning python3 -B -m unittest discover -s tests -v
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-optional.txt
.venv/bin/python -m company_kernel.company_service_smoke --json-only
bin/company-daemon --once --summary
bin/companyctl task submit --from openclaw-main --to codex --task-id task-daemon-worker-smoke --title "daemon worker smoke"
bin/company-daemon --once --enable-worker codex --summary
bin/companyctl task show --task-id task-daemon-worker-smoke
bin/companyctl doctor --summary --strict-launchd
bin/company-dashboard
python3 /Users/owner/openclaw/workspace-xmanx/scripts/company_runtime_alert.py --json-only
python3 /Users/owner/openclaw/workspace-xmanx/scripts/supervisor_heartbeat.py --json-only
python3 /Users/owner/openclaw/workspace-xmanx/scripts/heartbeat_summary_router.py --print-only
openclaw gateway probe
```

## Latest Verified Result

- Unit tests: 44/44 passing.
- Service smoke: REST OK, RPC OK, gRPC ready when `grpcio` is installed.
- Daemon worker smoke: verified in automated tests; manual command path documented in README.
- Strict doctor: `ok=true`, `issues=[]`.
- Launchd: installed=true, matches_template=true.
- Heartbeats: 15 active employee heartbeats, missing=0, stale=0.
- Telegram approval button: real click moved `company-kernel-telegram-real-button-click-smoke` from pending to approved; direct watcher launchd is disabled to keep OpenClaw/Hermes stable.
- Evidence health: 0 issues.
- Capability health: 0 issues.
- OpenClaw runtime alert: `severity=ok`.

## Runtime Note

The launchd service has been installed on the local Mac and verified with `bin/companyctl doctor --summary --strict-launchd`. For another Mac, run `bash bin/company-daemon-install-launchd` and verify with the same strict doctor command.

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
