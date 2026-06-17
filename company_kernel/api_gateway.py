from __future__ import annotations

import argparse
import contextlib
import hmac
import io
import json
import os
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import companyctl
from . import company_dashboard
from . import company_trace


CONSOLE_TEMPLATE = Path(
    os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])
).resolve() / "dashboard_templates" / "console.html"
CONSOLE_PATHS = {"/", "/console", "/ui", "/index.html"}

# Auth is opt-in: set COMPANY_KERNEL_API_TOKEN to require a Bearer token on every API
# call. When unset, the gateway stays open (relies on 127.0.0.1 binding) for backward
# compatibility. The console shell (GET /) and CORS preflight never require the token so
# the page can load and then prompt the user for it; all data/mutation endpoints do.
def api_token() -> str:
    return str(os.environ.get("COMPANY_KERNEL_API_TOKEN", "") or "").strip()


def bearer_token(headers) -> str:
    provided = str(headers.get("Authorization", "") or "")
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    return provided


def request_authorized(headers) -> bool:
    token = api_token()
    if not token:
        return True  # auth disabled (loopback self-host); set COMPANY_KERNEL_API_TOKEN to require auth
    # constant-time compare so the token can't be guessed via response timing
    provided = bearer_token(headers)
    return bool(provided) and hmac.compare_digest(provided, token)


# --- Human RBAC (opt-in, backward compatible) -------------------------------------------------
# Roles, low→high. A request is allowed if the actor's role rank >= the action's required rank.
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2, "owner": 3}


def load_users() -> dict:
    """config/users.json (per-deployment, gitignored): {"tokens": {"<bearer>": {"user":"alice","role":"operator"}}}.
    Present → multi-user RBAC. Absent → single-token / open mode (backward compatible)."""
    try:
        data = json.loads((companyctl.ROOT / "config" / "users.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_actor(headers) -> tuple[str | None, str]:
    """Return (user, role). role is '' when the request is NOT authorized.
    - users.json present → bearer must map to a user+role.
    - env token set (no users.json) → that token = owner (legacy single-token).
    - nothing configured → open self-host → owner (anonymous)."""
    users = load_users()
    tokens = users.get("tokens") if isinstance(users.get("tokens"), dict) else {}
    provided = bearer_token(headers)
    if tokens:
        if not provided:
            return None, ""
        for tok, info in tokens.items():
            if hmac.compare_digest(provided, str(tok)):
                role = str((info or {}).get("role") or "viewer")
                return str((info or {}).get("user") or "user"), (role if role in ROLE_RANK else "viewer")
        return None, ""
    env_token = api_token()
    if env_token:
        return ("owner", "owner") if (provided and hmac.compare_digest(provided, env_token)) else (None, "")
    return "anonymous", "owner"


def required_role(method: str, path: str) -> str:
    """Least role allowed to perform this request. Tasks/approvals/messages/conversations + pause/verify
    = operator; employee/runtime/settings config = admin; user management = owner; reads = viewer."""
    if method == "GET":
        return "viewer"
    if path.startswith("/v1/users"):
        return "owner"
    if path.startswith("/v1/employees/") and (path.endswith("/communication") or path.endswith("/verify-runtime")):
        return "operator"  # pause/resume + activate are operational controls
    if path.startswith("/v1/employees") or path.startswith("/v1/runtimes") or path.startswith("/v1/settings"):
        return "admin"     # create/onboard/offboard/profile/capabilities/permissions = config
    return "operator"      # dispatch / approve / message / conversation / etc.


API_VERSION = "v1"
ABSOLUTE_PATH_RE = re.compile(r"(?<![:\w.-])/[A-Za-z0-9_@%+=:,./~#-]+")
API_CAPABILITIES = [
    "health",
    "doctor",
    "tasks",
    "messages",
    "events",
    "conversations",
    "approvals",
    "heartbeats",
    "attendance",
    "adapter_runs",
    "projects",
    "locks",
    "followups",
    "employees",
    "runtimes",
    "skills",
    "sse_events",
    "trace_file_flow",
    "settings",
    "external_mirror",
    "openclaw_runtime_inventory",
    "operations_cockpit",
    "runtime_sessions",
    "tool_calls",
    "budget",
]
API_ENDPOINTS = [
    {"method": "GET", "path": "/v1/health", "summary": "Company Kernel health summary"},
    {"method": "GET", "path": "/v1/doctor", "summary": "Doctor summary", "query": {"strict_launchd": "bool optional", "strict_openclaw": "bool optional"}},
    {"method": "GET", "path": "/v1/employees", "summary": "List employees"},
    {"method": "POST", "path": "/v1/employees", "summary": "Create employee", "body": {"id": "employee id", "name": "display name", "role": "role", "runtime": "runtime id", "workspace": "path"}},
    {"method": "POST", "path": "/v1/employees/onboard", "summary": "Onboard employee with capabilities, permissions, communication, optional scaffold, and optional test task", "body": {"id": "employee id", "name": "display name", "role": "role", "runtime": "runtime id", "workspace": "path", "alias": "alias optional", "skills": "comma-separated optional", "tools": "comma-separated optional", "task_types": "comma-separated optional", "can_talk_to": "comma-separated optional", "can_assign_to": "comma-separated optional", "open_communication": "bool optional", "channel": "channel optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "create_test_task": "bool optional"}},
    {"method": "GET", "path": "/v1/employees/{employee_id}", "summary": "Show employee profile, capabilities, permissions, heartbeat, and files"},
    {"method": "GET", "path": "/v1/employees/{employee_id}/work-history", "summary": "Show employee work history, current activity, attempts, tool calls, budget, and evidence for the CEO cockpit"},
    {"method": "PATCH", "path": "/v1/employees/{employee_id}", "summary": "Update employee profile fields through companyctl", "body": {"name": "display name optional", "role": "role optional", "runtime": "runtime id optional", "workspace": "path optional", "status": "active/candidate/archived optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "dry_run": "bool optional"}},
    {"method": "DELETE", "path": "/v1/employees/{employee_id}", "summary": "Offboard employee with dry-run, soft archive, or guarded hard delete", "body": {"hard_delete": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/profile", "summary": "Update employee profile fields through companyctl", "body": {"name": "display name optional", "role": "role optional", "runtime": "runtime id optional", "workspace": "path optional", "status": "active/candidate/archived optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/verify-runtime", "summary": "Verify the employee's runtime and activate it if it passes (detached; poll /v1/employees for status)", "body": {"from": "source employee optional", "timeout": "seconds optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/offboard", "summary": "Offboard employee with dry-run, soft archive, or guarded hard delete", "body": {"hard_delete": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/communication", "summary": "Enable or pause employee communication policy", "body": {"enabled": "bool required", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/capabilities", "summary": "Update employee capabilities", "body": {"set_skills": "comma-separated skills optional", "add_skill": "string/list optional", "set_tools": "comma-separated tools optional", "add_tool": "string/list optional", "set_task_types": "comma-separated task types optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/permissions", "summary": "Update employee permissions", "body": {"can_submit_tasks": "true/false/keep optional", "can_claim_tasks": "true/false/keep optional", "can_modify_kernel": "true/false/keep optional", "requires_approval_for": "comma-separated actions optional"}},
    {"method": "POST", "path": "/v1/employees/match", "summary": "Rank employees by capabilities for routing", "body": {"skills": "comma-separated skills optional", "tools": "comma-separated tools optional", "task_type": "string optional", "runtime": "runtime optional", "role": "role optional", "limit": "integer optional", "include_unavailable": "bool optional"}},
    {"method": "GET", "path": "/v1/skills", "summary": "List local Skill Package manifests for AI Fleet & Skills"},
    {"method": "GET", "path": "/v1/settings/notification", "summary": "Read sanitized notification settings without secrets"},
    {"method": "POST", "path": "/v1/settings/notification", "summary": "Configure employee notification account without storing tokens", "body": {"telegram_account": "account id", "telegram_bot_token_env": "environment variable name containing token", "telegram_default_target": "chat/user target optional", "employee_notifications_enabled": "bool optional"}},
    {"method": "POST", "path": "/v1/notifications/send", "summary": "Send configured operator notification without exposing secrets", "body": {"message": "string required", "kind": "general/approval/error optional", "subject": "string optional", "target": "telegram target optional", "account": "account optional", "dry_run": "bool optional"}},
    {"method": "GET", "path": "/v1/progress/notifications", "summary": "Read pending or recent progress transition notifications", "query": {"pending_only": "bool optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/openclaw/runtime-inventory", "summary": "Read-only discovered OpenClaw agents, sessions, Telegram spools, and Company Kernel registration gaps"},
    {"method": "GET", "path": "/v1/openclaw/native-status", "summary": "Read-only OpenClaw native agent_bus, approvals, and supervisor status mapped for Company Kernel observability"},
    {"method": "POST", "path": "/v1/openclaw/dispatch-plan", "summary": "Dry-run an official OpenClaw agent_bus submit payload without mutating OpenClaw", "body": {"source": "OpenClaw source agent", "target": "OpenClaw target agent", "type": "agent_bus task type", "priority": "P0/P1/P2/P3 optional", "goal": "task goal", "next_command": "required safe next command", "expected_evidence": "required acceptance evidence", "rollback": "required rollback plan", "task_id": "Kernel task id optional"}},
    {"method": "POST", "path": "/v1/openclaw/dispatch-execute", "summary": "Write an official OpenClaw agent_bus inbox file only after owner approval", "body": {"source": "OpenClaw source agent", "target": "OpenClaw target agent", "type": "agent_bus task type", "priority": "P0/P1/P2/P3 optional", "goal": "task goal", "next_command": "required safe next command", "expected_evidence": "required acceptance evidence", "rollback": "required rollback plan", "approval_id": "approved openclaw_native_dispatch approval id", "task_id": "Kernel task id optional"}},
    {"method": "POST", "path": "/v1/openclaw/import-results", "summary": "Import OpenClaw native done/failed result files into Kernel ledger without mutating OpenClaw", "body": {"limit": "integer optional", "agent": "OpenClaw result agent optional"}},
    {"method": "GET", "path": "/v1/supervisor/delivery-loop", "summary": "Read latest autonomous supervisor delivery-loop result"},
    {"method": "POST", "path": "/v1/supervisor/delivery-loop", "summary": "Run autonomous supervisor delivery-loop once", "body": {"limit": "integer optional", "by": "actor optional"}},
    {"method": "POST", "path": "/v1/policy-blocks/report", "summary": "Report non-popup tool-policy blockers and notify operator", "body": {"source": "employee optional", "target": "employee optional", "tool": "tool name optional", "operation": "operation optional", "error": "error text required", "dry_run": "bool optional"}},
    {"method": "GET", "path": "/v1/runtimes", "summary": "List runtimes"},
    {"method": "POST", "path": "/v1/runtimes", "summary": "Register runtime", "body": {"runtime": "runtime id", "command": "command optional", "status": "registered/disabled optional", "notes": "string optional"}},
    {"method": "GET", "path": "/v1/runtime-sessions", "summary": "List managed runtime sessions", "query": {"employee_id": "employee optional", "task_id": "task optional", "trace_id": "trace optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/runtime-sessions/{session_id}", "summary": "Show one runtime session with related task, attempt, tool calls, budget, evidence, and events"},
    {"method": "GET", "path": "/v1/tool-calls", "summary": "List structured agent tool calls", "query": {"employee_id": "employee optional", "task_id": "task optional", "trace_id": "trace optional", "attempt_id": "attempt optional", "session_id": "runtime session optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/tool-calls/{tool_call_id}", "summary": "Show one sanitized tool call with related task, attempt, runtime session, budget, evidence, and events"},
    {"method": "GET", "path": "/v1/budget-events", "summary": "List budget/cost ledger events", "query": {"employee_id": "employee optional", "task_id": "task optional", "trace_id": "trace optional", "attempt_id": "attempt optional", "limit": "integer optional"}},
    {"method": "POST", "path": "/v1/budget-events", "summary": "Record a budget/cost ledger event for adapter, tool, or model usage", "body": {"employee_id": "employee id", "task_id": "task optional", "attempt_id": "attempt optional", "trace_id": "trace optional", "cost_type": "model_api/tool_runtime/compute/etc", "amount": "number", "currency": "USD optional", "token_input": "integer optional", "token_output": "integer optional", "model_name": "optional", "provider": "optional", "runtime_seconds": "integer optional", "summary": "optional"}},
    {"method": "GET", "path": "/v1/budget-summary", "summary": "Read budget rollup for owner cockpit", "query": {"employee_id": "employee optional", "task_id": "task optional", "trace_id": "trace optional", "attempt_id": "attempt optional"}},
    {"method": "GET", "path": "/v1/economics", "summary": "Per-task-type unit economics: revenue vs cost vs margin (survival metric #1)"},
    {"method": "GET", "path": "/v1/verifier-accuracy", "summary": "Per-kind verifier sampling accuracy vs human review (survival metric #2)"},
    {"method": "GET", "path": "/v1/tasks", "summary": "List tasks", "query": {"agent": "employee id optional", "status": "task status optional"}},
    {"method": "POST", "path": "/v1/tasks", "summary": "Submit task", "body": {"from": "employee id", "to": "employee id", "title": "string", "description": "string optional", "task_id": "string optional", "priority": "P0/P1/P2/P3 optional", "requires_approval": "action optional", "approval_id": "string optional"}},
    {"method": "POST", "path": "/v1/tasks/route", "summary": "Select employee by capabilities and submit routed task with approval guard", "body": {"from": "employee id", "title": "string", "description": "string optional", "priority": "P0/P1/P2/P3 optional", "task_id": "string optional", "skills": "comma-separated skills optional", "tools": "comma-separated tools optional", "task_type": "string optional", "runtime": "runtime optional", "role": "role optional", "limit": "integer optional", "include_unavailable": "bool optional", "requires_approval": "action optional", "approval_id": "string optional", "risk": "P0/P1/P2/P3 optional", "changed_files": "comma-separated paths optional", "rfc": "path optional"}},
    {"method": "GET", "path": "/v1/tasks/{task_id}", "summary": "Show task"},
    {"method": "POST", "path": "/v1/tasks/{task_id}/claim", "summary": "Claim task", "body": {"agent": "employee id", "lease_seconds": "integer optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/done", "summary": "Complete task", "body": {"agent": "employee id", "summary": "string", "evidence": "path"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/block", "summary": "Block task", "body": {"agent": "employee id", "blocker": "string"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/reopen", "summary": "Reopen blocked/interrupted task", "body": {"by": "employee id", "reason": "string", "status": "submitted/claimed optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/retry", "summary": "Retry failed/stale/cancelled managed task with a new attempt", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/reassign", "summary": "Reassign task to another employee", "body": {"by": "employee id", "to": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/run", "summary": "Start managed long-running attempt", "body": {"agent": "employee id", "by": "employee id", "max_runtime_seconds": "integer optional", "stale_after_seconds": "integer optional", "session_key": "string optional", "pid": "string optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/progress", "summary": "Record managed attempt progress and refresh stale clock", "body": {"agent": "employee id", "attempt_id": "attempt id optional", "state": "progress state optional", "message": "string", "progress": "integer optional", "payload": "object optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/probe", "summary": "Record owner/supervisor progress probe without mutating worker attempt progress", "body": {"by": "employee id", "attempt_id": "attempt id optional", "message": "string", "reason": "string optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/correct", "summary": "Send supervisor correction or correction ack", "body": {"attempt_id": "attempt id", "by": "employee id", "message": "string", "ack": "bool optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/cancel", "summary": "Cancel managed long-running attempt", "body": {"attempt_id": "attempt id", "by": "employee id", "reason": "string"}},
    {"method": "GET", "path": "/v1/tasks/{task_id}/attempts", "summary": "List managed execution attempts"},
    {"method": "GET", "path": "/v1/tasks/{task_id}/conversations", "summary": "List task-bound conversations"},
    {"method": "POST", "path": "/v1/tasks/{task_id}/conversations", "summary": "Start task-bound conversation", "body": {"from": "employee id optional", "participants": "comma-separated extra participants optional", "title": "string optional", "body": "string optional", "conversation_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/messages", "summary": "List messages", "query": {"agent": "employee id required"}},
    {"method": "GET", "path": "/v1/messages/recent-direct", "summary": "Dashboard-ready recent direct messages feed", "query": {"limit": "integer optional"}},
    {"method": "GET", "path": "/v1/events", "summary": "List Company Kernel event ledger entries", "query": {"pending_only": "bool optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/events/stream", "summary": "Server-Sent Events stream for recent Company Kernel event ledger entries", "query": {"limit": "integer optional", "poll_seconds": "integer optional", "max_cycles": "integer optional"}},
    {"method": "GET", "path": "/v1/evidence", "summary": "List sanitized evidence records for Audit Hub", "query": {"task_id": "task id optional", "employee_id": "employee id optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/evidence/{evidence_id}/content", "summary": "Read safe text preview for a whitelisted evidence record without exposing absolute paths"},
    {"method": "GET", "path": "/v1/evidence/{evidence_id}/safe-preview", "summary": "Alias for safe evidence text preview; enforces the same whitelist and secret-path policy"},
    {"method": "POST", "path": "/v1/evidence/{evidence_id}/accept", "summary": "Owner accepts task-bound final evidence after safe preview", "body": {"by": "employee id", "summary": "acceptance summary optional"}},
    {"method": "POST", "path": "/v1/evidence/{evidence_id}/reject", "summary": "Owner rejects task-bound final evidence and records reason", "body": {"by": "employee id", "reason": "rejection reason"}},
    {"method": "GET", "path": "/v1/artifacts", "summary": "List sanitized artifact records for Audit Hub", "query": {"task_id": "task id optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/handoffs", "summary": "List handoff contracts for Audit Hub", "query": {"task_id": "from or to task id optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/failures", "summary": "List sanitized task, attempt, and adapter failure records for Audit Hub", "query": {"task_id": "task id optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/traces/{trace_id}/timeline", "summary": "Read sanitized trace timeline for dashboard trace view"},
    {"method": "GET", "path": "/v1/traces/{trace_id}/file-flow", "summary": "Read trace task/artifact/handoff/evidence file-flow graph"},
    {"method": "GET", "path": "/v1/workspaces/prune", "summary": "Preview task workspace retention prune candidates; dry-run only", "query": {"dry_run": "bool required", "older_than_days": "integer optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/dashboard/communication-observability", "summary": "Dashboard-ready summary for direct messages, external mirror status, adapter-run progress, 5-layer progress heartbeat, and internal no-receipt watchdog"},
    {"method": "GET", "path": "/v1/dashboard/cockpit", "summary": "Dashboard-ready AI Employee Cockpit summary with long-task heartbeat/progress state and sanitized evidence"},
    {"method": "GET", "path": "/v1/dashboard/internal-watchdog", "summary": "Detect internal messages/tasks that were delivered but have no receipt, claim, or final evidence"},
    {"method": "POST", "path": "/v1/dashboard/internal-watchdog/remediate", "summary": "Create or dry-run follow-up/escalation/reroute-decision actions for no-receipt messages and open internal tasks", "body": {"source": "employee id optional", "dry_run": "bool optional default true", "deliver": "bool optional", "escalate_to": "employee id optional default hermes", "escalate_existing": "bool optional", "reroute_to": "employee id optional default codex", "create_reroute_plan": "bool optional"}},
    {"method": "POST", "path": "/v1/dashboard/internal-watchdog/apply-reroutes", "summary": "Apply answered reroute decisions by creating new tasks and blocking original stalled tasks", "body": {"by": "employee id optional default hermes", "dry_run": "bool optional default true"}},
    {"method": "POST", "path": "/v1/messages", "summary": "Send message", "body": {"from": "employee id", "to": "employee id", "body": "string", "message_id": "string optional"}},
    {"method": "POST", "path": "/v1/messages/direct", "summary": "Directly invoke target employee runtime and record message evidence", "body": {"from": "employee id", "to": "employee id", "body": "string", "message_id": "string optional", "session_key": "string optional", "timeout": "integer optional", "deliver": "bool optional", "reply_channel": "string optional", "reply_to": "string optional", "reply_account": "string optional"}},
    {"method": "GET", "path": "/v1/followups", "summary": "List followups", "query": {"status": "pending/answered/cancelled/all optional"}},
    {"method": "POST", "path": "/v1/followups", "summary": "Create followup question", "body": {"from": "employee id", "to": "employee id", "question": "string", "context": "string optional", "followup_id": "string optional", "deliver": "bool optional", "reply_channel": "string optional", "reply_account": "string optional", "reply_to": "string optional"}},
    {"method": "GET", "path": "/v1/followups/{followup_id}", "summary": "Show followup"},
    {"method": "POST", "path": "/v1/followups/{followup_id}/reply", "summary": "Reply to followup and continue agent delivery", "body": {"by": "employee id", "answer": "string", "message_id": "string optional"}},
    {"method": "GET", "path": "/v1/conversations", "summary": "List conversations for an agent", "query": {"agent": "employee id required"}},
    {"method": "POST", "path": "/v1/conversations", "summary": "Start conversation", "body": {"from": "employee id", "participants": "comma-separated employee ids", "title": "string", "body": "string", "conversation_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/conversations/{conversation_id}", "summary": "Show conversation"},
    {"method": "GET", "path": "/v1/external-threads", "summary": "List sanitized external mirror threads", "query": {"platform": "platform optional", "owner_agent": "owner agent optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/external-threads/{thread_id}", "summary": "Show sanitized external mirror thread and messages"},
    {"method": "GET", "path": "/v1/external-threads/{thread_id}/messages", "summary": "List sanitized external mirror messages"},
    {"method": "POST", "path": "/v1/external-mirror/import", "summary": "Import sanitized external mirror payload without secrets", "body": {"thread": "sanitized thread object", "messages": "sanitized messages list"}},
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/join", "summary": "Join an existing conversation as Human Owner or another employee", "body": {"agent": "employee id optional, defaults owner-shift"}},
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/reply", "summary": "Reply to conversation", "body": {"from": "employee id", "body": "string", "message_id": "string optional", "evidence": "path optional"}},
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/run", "summary": "Run an autonomous multi-employee meeting/discussion that converges to minutes/a plan", "body": {"mode": "meeting/discuss/standup optional", "rounds": "integer optional, default 2", "timeout": "per-turn seconds optional", "synthesizer": "chair employee id optional, defaults hermes"}},
    {"method": "POST", "path": "/v1/conversations/probe", "summary": "Test which employees can genuinely join a meeting and persist the allowlist", "body": {"participants": "'active' (default), 'all', or comma-separated ids", "timeout": "per-probe seconds optional"}},
    {"method": "GET", "path": "/v1/approvals", "summary": "List approvals", "query": {"status": "pending/approved/denied/all optional", "agent": "employee id optional", "action": "approval action optional", "limit": "integer optional"}},
    {"method": "POST", "path": "/v1/approvals", "summary": "Request approval", "body": {"from": "employee id", "action": "string", "reason": "string", "target": "employee id optional", "risk": "P0/P1/P2/P3 optional", "approval_id": "string optional", "task_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/approvals/{approval_id}", "summary": "Show approval"},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/approve", "summary": "Approve request", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/deny", "summary": "Deny request", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/resolve", "summary": "Mock-resolve request without external delivery", "body": {"by": "employee id", "reason": "string", "mock": True}},
    {"method": "POST", "path": "/v1/heartbeats", "summary": "Write employee heartbeat", "body": {"agent": "employee id"}},
    {"method": "GET", "path": "/v1/heartbeats", "summary": "List all employee heartbeats"},
    {"method": "GET", "path": "/v1/events", "summary": "List recent company events", "query": {"limit": "integer optional, default 50, max 200"}},
    {"method": "GET", "path": "/", "summary": "Live operations console (HTML), also at /console"},
    {"method": "GET", "path": "/v1/attendance/latest", "summary": "Read latest persisted attendance sweep report"},
    {"method": "POST", "path": "/v1/attendance/sweep", "summary": "Run attendance sweep with optional exact agent reply probes", "body": {"source": "source employee optional", "agents": "comma-separated employees optional", "sweep_id": "string optional", "include_candidates": "bool optional", "stale_minutes": "integer optional", "probe_replies": "bool optional", "reply_timeout": "integer optional"}},
    {"method": "GET", "path": "/v1/agent-matrix", "summary": "Read layered employee readiness matrix", "query": {"agents": "comma-separated employees optional"}},
    {"method": "GET", "path": "/v1/projects", "summary": "List projects", "query": {"status": "active/paused/completed/blocked/all optional"}},
    {"method": "POST", "path": "/v1/projects", "summary": "Create project", "body": {"project_id": "string optional", "title": "string", "goal": "string optional", "owner": "employee id", "status": "active/paused/completed/blocked optional", "acceptance": "semicolon-separated criteria optional"}},
    {"method": "GET", "path": "/v1/projects/{project_id}", "summary": "Show project with linked tasks and plan items"},
    {"method": "POST", "path": "/v1/projects/{project_id}/tasks", "summary": "Link task to project", "body": {"task_id": "string"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/plan-items", "summary": "Add project plan item", "body": {"title": "string", "status": "planned/in_progress/done/completed/blocked/cancelled optional", "owner": "employee id optional", "due_at": "string optional", "task_id": "string optional", "plan_id": "string optional"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/plan-items/{plan_id}/status", "summary": "Update project plan item status", "body": {"status": "planned/in_progress/done/completed/blocked/cancelled"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/status", "summary": "Update project status", "body": {"status": "active/paused/completed/blocked"}},
    {"method": "GET", "path": "/v1/projects/{project_id}/review", "summary": "Review project readiness"},
    {"method": "POST", "path": "/v1/projects/{project_id}/accept", "summary": "Accept project completion", "body": {"by": "employee id", "summary": "string", "force": "bool optional"}},
    {"method": "GET", "path": "/v1/locks", "summary": "List locks", "query": {"agent": "employee id optional"}},
    {"method": "POST", "path": "/v1/locks/acquire", "summary": "Acquire lock", "body": {"agent": "employee id", "resource": "resource key", "lease_seconds": "integer optional"}},
    {"method": "POST", "path": "/v1/locks/release", "summary": "Release lock", "body": {"agent": "employee id", "resource": "resource key", "force": "bool optional"}},
    {"method": "POST", "path": "/v1/locks/unlock-stale", "summary": "Unlock stale locks"},
    {"method": "GET", "path": "/v1/adapter-runs", "summary": "List adapter runs", "query": {"agent": "employee id optional", "status": "ok/failed optional", "unacknowledged_only": "bool optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/adapter-runs/{run_id}", "summary": "Show adapter run", "query": {"summary": "bool optional"}},
    {"method": "POST", "path": "/v1/adapter-runs/{run_id}/ack", "summary": "Acknowledge adapter failure", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/adapter-runs/{run_id}/retry", "summary": "Retry failed adapter task", "body": {"by": "employee id", "reason": "string", "task_id": "string optional"}},
]


def service_descriptor() -> dict:
    return {
        "ok": True,
        "name": "Company Kernel API Gateway",
        "version": API_VERSION,
        "capabilities": API_CAPABILITIES,
        "links": {
            "self": "/v1",
            "health": "/v1/health",
            "openapi": "/v1/openapi.json",
            "rpc": "/rpc",
            "grpc_proto": "docs/company_kernel.proto",
        },
        "protocols": {
            "rest": True,
            "json_rpc": True,
            "grpc": "optional-grpcio",
        },
        "endpoints": API_ENDPOINTS,
        "governance": {
            "state_writer": "companyctl",
            "direct_sqlite_writes": False,
            "task_completion_requires": "evidence_or_blocker",
            "high_risk_requires_approval": True,
        },
    }


def openapi_descriptor() -> dict:
    paths: dict[str, dict[str, dict]] = {}
    for endpoint in API_ENDPOINTS:
        path = endpoint["path"]
        method = endpoint["method"].lower()
        operation = {
            "summary": endpoint["summary"],
            "responses": {
                "200": {"description": "OK"},
                "201": {"description": "Created"},
                "400": {"description": "Bad Request"},
                "404": {"description": "Not Found"},
            },
        }
        if "query" in endpoint:
            operation["parameters"] = [
                {"name": name, "in": "query", "required": "required" in detail, "schema": {"type": "string"}, "description": detail}
                for name, detail in endpoint["query"].items()
            ]
        if "body" in endpoint:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {name: {"type": "string", "description": detail} for name, detail in endpoint["body"].items()},
                        }
                    }
                },
            }
        paths.setdefault(path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "Company Kernel API Gateway", "version": API_VERSION},
        "paths": paths,
    }


_CLI_LOCK = threading.Lock()


def run_companyctl(argv: list[str]) -> tuple[int, dict]:
    # ThreadingHTTPServer handles requests concurrently, but redirect_stdout swaps the
    # process-global sys.stdout: without a lock, parallel requests corrupt each other's
    # captured output and return empty payloads. Serialize all in-process CLI calls.
    with _CLI_LOCK:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = companyctl.main(argv)
        raw = buf.getvalue().strip()
    return code, json.loads(raw) if raw else {}


def spawn_conversation_run(conversation_id: str, body: dict) -> tuple[bool, dict]:
    """Launch `companyctl conversation run` as a detached background process so the
    console stays responsive while employees discuss. Output goes to a per-run log."""
    cid = re.sub(r"[^A-Za-z0-9_.-]", "", str(conversation_id))
    if not cid:
        return False, {"ok": False, "error": "invalid conversation id"}
    argv = [str(companyctl.ROOT / "bin" / "companyctl"), "conversation", "run", "--conversation-id", cid]
    if body.get("mode"):
        argv.extend(["--mode", str(body["mode"])])
    if body.get("rounds") not in {None, ""}:
        argv.extend(["--rounds", str(int(body["rounds"]))])
    if body.get("timeout") not in {None, ""}:
        argv.extend(["--timeout", str(int(body["timeout"]))])
    if body.get("synthesizer"):
        argv.extend(["--synthesizer", str(body["synthesizer"])])
    log_dir = companyctl.ROOT / "logs" / "conversation-run"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{cid}.log"
        log_fh = open(log_path, "ab")
    except Exception as exc:
        return False, {"ok": False, "error": f"cannot open run log: {exc}"}
    try:
        subprocess.Popen(
            argv,
            cwd=str(companyctl.ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log_fh.close()
        return False, {"ok": False, "error": str(exc)}
    finally:
        # The child inherits its own dup of the fd; the parent's copy can close.
        try:
            log_fh.close()
        except Exception:
            pass
    return True, {"ok": True, "started": True, "conversation_id": cid, "log": str(log_path)}


def spawn_companyctl_detached(argv: list[str], log_path: Path) -> tuple[bool, dict]:
    """Run a companyctl invocation as a detached background process so a slow command never
    holds the in-process CLI lock and freezes the gateway for every other request."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "ab")
    except Exception as exc:
        return False, {"ok": False, "error": f"cannot open log: {exc}"}
    try:
        subprocess.Popen(
            [str(companyctl.ROOT / "bin" / "companyctl"), *argv],
            cwd=str(companyctl.ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as exc:
        log_fh.close()
        return False, {"ok": False, "error": str(exc)}
    finally:
        try: log_fh.close()
        except Exception: pass
    return True, {"ok": True, "started": True, "log": str(log_path)}


def query_value(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name, [])
    return values[0] if values else default


def body_values(body: dict, name: str) -> list[str]:
    value = body.get(name)
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def truthy(value: object) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes", "on"}


def with_control_action(
    payload: dict,
    *,
    action: str,
    event_type: str,
    requires_owner_approval: bool = True,
    dangerous: bool = False,
) -> dict:
    event_id = str(payload.get("event_id", "") or "")
    task_id = str(payload.get("task_id") or payload.get("task", {}).get("id") or "")
    attempt_id = str(payload.get("attempt", {}).get("attempt_id") or "")
    return {
        **payload,
        "control_action": {
            "action": action,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "event_type": event_type,
            "event_id": event_id,
            "requires_owner_approval": requires_owner_approval,
            "approval_required": requires_owner_approval,
            "approval_mode": "owner_required_for_real_execution",
            "dangerous": dangerous,
            "audit": {
                "recorded": bool(event_id),
                "event_id": event_id,
                "ledger": "company_events",
            },
        },
    }


def attach_task_control_context(payload: dict, task_id: str) -> dict:
    if not task_id:
        return payload
    conn = companyctl.connect()
    try:
        context = companyctl.task_control_context_bundle(conn, task_id)
    finally:
        conn.close()
    return {**payload, "control_context": context}


def control_action_approval_response(
    *,
    task_id: str,
    action: str,
    by: str,
    reason: str,
    attempt_id: str = "",
    target: str = "",
    risk: str = "P1",
    dangerous: bool = False,
) -> tuple[int, dict]:
    approval_action = f"task_control.{action}"
    approval_id = f"approval-{approval_action.replace('.', '-')}-{task_id}"
    metadata = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "control_action": action,
        "execute_requires": "execute=true or approved owner action",
    }
    if target:
        metadata["target"] = target
    conn = companyctl.connect()
    try:
        result = companyctl.create_approval_internal(
            conn,
            source=by,
            action=approval_action,
            reason=reason,
            target=target,
            risk=risk,
            approval_id=approval_id,
            metadata=metadata,
        )
    finally:
        conn.close()
    event_id = str(result.get("event", {}).get("id", "") or "")
    approval = dict(result.get("approval", {}) or {})
    detail = approval.get("detail", {}) if isinstance(approval.get("detail", {}), dict) else {}
    approval["metadata"] = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
    payload = {
        "ok": False,
        "executed": False,
        "approval_required": True,
        "approval": approval,
        "notification": result.get("notification", {}),
        "event": result.get("event", {}),
    }
    response = with_control_action(
        payload,
        action=action,
        event_type="approval.requested",
        requires_owner_approval=True,
        dangerous=dangerous,
    )
    response["control_action"]["event_id"] = event_id
    response["control_action"]["approval_mode"] = "pending_owner_approval"
    response["control_action"]["audit"] = {"recorded": bool(event_id), "event_id": event_id, "ledger": "company_events"}
    response = attach_task_control_context(response, task_id)
    return HTTPStatus.ACCEPTED, response


PATH_LIKE_EVENT_KEYS = {
    "artifact",
    "artifact_path",
    "evidence",
    "evidence_path",
    "file",
    "files",
    "path",
    "path_or_url",
    "profile",
    "state_file",
    "original_path",
}
PATH_DISPLAY_KEYS = {
    "artifact",
    "artifact_path",
    "evidence",
    "evidence_path",
    "file",
    "files",
    "path",
    "path_or_url",
    "profile",
    "state_file",
    "original_path",
}


def sanitize_event_value(value: object, *, key: str = "") -> object:
    key_lower = key.lower()
    if isinstance(value, dict):
        return {str(item_key): sanitize_event_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_event_value(item, key=key) for item in value]
    if isinstance(value, str):
        if key_lower in PATH_DISPLAY_KEYS:
            display = companyctl.sanitize_evidence_path_for_display(value)
            return {
                "relative_path": display.get("relative_path", ""),
                "basename": display.get("basename", ""),
                "allowed": display.get("allowed", False),
                "reason": display.get("reason", ""),
                "absolute_path_exposed": False,
            }
        return companyctl.sanitize_log_text(value)
    return value


def sanitize_event_row(row: dict) -> dict:
    event = dict(row)
    raw_payload = event.get("payload_json", "")
    if raw_payload:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            payload = companyctl.sanitize_log_text(raw_payload)
        event["payload"] = sanitize_event_value(payload)
        event["payload_json"] = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True)
    else:
        event["payload"] = {}
        event["payload_json"] = ""
    return event


def sanitize_task_list_payload(payload: dict) -> dict:
    result = dict(payload)
    tasks = result.get("tasks", [])
    if not isinstance(tasks, list):
        return result
    sanitized_tasks: list[dict] = []
    for task in tasks:
        if not isinstance(task, dict):
            sanitized_tasks.append(task)
            continue
        item = dict(task)
        raw_evidence_path = str(item.pop("evidence_path", "") or "")
        item["evidence"] = companyctl.sanitize_evidence_path_for_display(raw_evidence_path)
        for key in ("title", "description", "summary", "blocker"):
            if key in item:
                item[key] = ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", companyctl.sanitize_log_text(item.get(key, "")))
        if isinstance(item.get("blocker_triage"), dict) and item["blocker_triage"].get("reason"):
            item["blocker_triage"] = {**item["blocker_triage"],
                "reason": ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", companyctl.sanitize_log_text(item["blocker_triage"]["reason"]))}
        if isinstance(item.get("current_attempt"), dict):
            item["current_attempt"] = companyctl.sanitize_json_like(item["current_attempt"])
        sanitized_tasks.append(item)
    result["tasks"] = sanitized_tasks
    return result


def sanitize_dashboard_text(value: object) -> str:
    return ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", companyctl.sanitize_log_text(value))


def sanitize_api_display_value(value: object, *, key: str = "") -> object:
    key_lower = key.lower()
    if key_lower in {"payload_json", "detail_json", "result_json", "metadata_json"}:
        return None
    if isinstance(value, dict):
        if value.get("absolute_path_exposed") is False and {"path", "relative_path", "basename", "allowed", "reason"}.issubset(value.keys()):
            return value
        sanitized = {}
        for item_key, item_value in value.items():
            clean_value = sanitize_api_display_value(item_value, key=str(item_key))
            if clean_value is not None:
                sanitized[str(item_key)] = clean_value
        return sanitized
    if isinstance(value, list):
        return [item for item in (sanitize_api_display_value(item, key=key) for item in value) if item is not None]
    if isinstance(value, str):
        if key_lower in PATH_DISPLAY_KEYS:
            display = companyctl.sanitize_evidence_path_for_display(value)
            return {
                "relative_path": display.get("relative_path", ""),
                "basename": display.get("basename", ""),
                "allowed": display.get("allowed", False),
                "reason": display.get("reason", ""),
                "absolute_path_exposed": False,
            }
        return sanitize_dashboard_text(value)
    return value


def sanitize_task_detail_payload(payload: dict) -> dict:
    result = sanitize_api_display_value(payload)
    if not isinstance(result, dict):
        return {}
    task = result.get("task")
    if isinstance(task, dict):
        raw_evidence_path = ""
        original_task = payload.get("task", {}) if isinstance(payload.get("task"), dict) else {}
        if isinstance(original_task, dict):
            raw_evidence_path = str(original_task.get("evidence_path") or "")
        task.pop("evidence_path", None)
        task["evidence"] = companyctl.sanitize_evidence_path_for_display(raw_evidence_path)
    return result


def recent_event_rows(*, limit: int = 20, after_created_at: str = "", after_id: str = "") -> list[dict]:
    conn = companyctl.connect_readonly()
    try:
        limit = max(1, min(int(limit), 200))
        if after_id:
            anchor = conn.execute("SELECT rowid FROM company_events WHERE id = ?", (after_id,)).fetchone()
            if anchor:
                return companyctl.rows(
                    conn,
                    """
                    SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
                    FROM company_events
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                    LIMIT ?
                    """,
                    (anchor["rowid"], limit),
                )
        if after_created_at:
            return companyctl.rows(
                conn,
                """
                SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
                FROM company_events
                WHERE created_at > ? OR (created_at = ? AND id > ?)
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (after_created_at, after_created_at, after_id, limit),
            )
        latest = companyctl.rows(
            conn,
            """
            SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
            FROM company_events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return list(reversed(latest))
    finally:
        conn.close()


def sanitize_payload_values(obj):
    """Recursively sanitize secrets/local-paths in payload STRING VALUES only — keeping the JSON
    structure intact. (Sanitizing the serialized JSON string then re-parsing it can truncate a value
    mid-quote and raise 'Unterminated string', breaking the whole /v1/events feed.)"""
    if isinstance(obj, dict):
        return {k: sanitize_payload_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload_values(v) for v in obj]
    if isinstance(obj, str):
        return companyctl.sanitize_log_text(obj)
    return obj


def route_get(path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
    if path in {"/v1", "/v1/"}:
        return HTTPStatus.OK, service_descriptor()
    if path == "/v1/openapi.json":
        return HTTPStatus.OK, openapi_descriptor()
    if path in {"/health", "/v1/health"}:
        code, payload = run_companyctl(["doctor", "--summary"])
        return HTTPStatus.OK, {"exit_code": code, **payload}
    if path == "/v1/approval/mode":
        code, payload = run_companyctl(["approval", "mode"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path == "/v1/memory/projects":
        code, payload = run_companyctl(["memory", "project", "list"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path.startswith("/v1/memory/projects/"):
        pid = path.removeprefix("/v1/memory/projects/").strip("/")
        code, payload = run_companyctl(["memory", "project", "show", "--id", pid])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.NOT_FOUND), payload
    if path in {"/v1/doctor", "/doctor"}:
        argv = ["doctor", "--summary"]
        if query_value(query, "strict_launchd") in {"1", "true", "yes"}:
            argv.append("--strict-launchd")
        if query_value(query, "strict_openclaw") in {"1", "true", "yes"}:
            argv.append("--strict-openclaw")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/employees":
        conn = companyctl.connect_readonly()
        try:
            summary = company_dashboard.load_summary(conn)
            employees = company_dashboard.employee_view_models(summary)
            return HTTPStatus.OK, {"exit_code": 0, "ok": True, "employees": employees}
        finally:
            conn.close()
    if path == "/v1/employees/match":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "use POST", "path": path}
    if path == "/v1/skills":
        return HTTPStatus.OK, companyctl.skill_registry()
    if path == "/v1/settings/notification":
        return HTTPStatus.OK, companyctl.notification_settings()
    if path.startswith("/v1/employees/") and path.endswith("/work-history"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/work-history").strip("/")
        conn = companyctl.connect()
        try:
            bundle = companyctl.employee_file_bundle(conn, employee_id)
        finally:
            conn.close()
        return HTTPStatus.OK, {
            "ok": True,
            "source": "/v1/employees/{employee_id}/work-history",
            "employee_id": employee_id,
            "employee": bundle.get("employee", {}),
            "current_activity": bundle.get("current_activity", {}),
            "operational_summary": bundle.get("operational_summary", {}),
            "ceo_work_contract": bundle.get("ceo_work_contract", {}),
            "work_history": bundle.get("work_history", {}),
            "attempts": bundle.get("attempts", []),
            "runtime_sessions": bundle.get("runtime_sessions", []),
            "tool_calls": bundle.get("tool_calls", []),
            "budget_summary": bundle.get("budget_summary", {}),
            "budget_events": bundle.get("budget_events", []),
            "evidence_records": bundle.get("evidence_records", []),
        }
    if path.startswith("/v1/employees/"):
        employee_id = path.removeprefix("/v1/employees/").strip("/")
        code, payload = run_companyctl(["employee", "show", "--id", employee_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/runtimes":
        code, payload = run_companyctl(["runtime", "list"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/events":
        try:
            limit = max(1, min(int(query_value(query, "limit") or 50), 200))
        except ValueError:
            limit = 50
        conn = companyctl.connect()
        try:
            rows = conn.execute(
                "SELECT id, event_type, source_agent, task_id, created_at, processed_at, trace_id, payload_json FROM company_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            event = dict(row)
            raw = event.pop("payload_json", "") or ""
            try:
                payload = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                payload = {"raw": str(raw)}
            # Sanitize secrets / local paths before exposing over the API (per-value, never breaks JSON).
            event["payload"] = sanitize_payload_values(payload)
            events.append(event)
        return HTTPStatus.OK, {"ok": True, "events": events}
    if path == "/v1/heartbeats":
        conn = companyctl.connect()
        try:
            rows = conn.execute(
                "SELECT agent_id, runtime, workspace, status, last_seen_at FROM heartbeats ORDER BY agent_id"
            ).fetchall()
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "heartbeats": [dict(row) for row in rows]}
    if path == "/v1/employees/offline-report":
        argv = ["employee", "offline-report"]
        if query_value(query, "stale_minutes"):
            argv.extend(["--stale-minutes", str(query_value(query, "stale_minutes"))])
        if query_value(query, "notify") in ("1", "true", "yes"):
            argv.append("--notify")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/delivery-targets":
        # 群投递目标:供控制台 @mention 自动补全(@agent.group)。读各 OpenClaw agent 的注册表。
        from . import openclaw_adapter
        agent = query_value(query, "agent")
        agents = [agent] if agent else [e["id"] for e in (run_companyctl(["employee", "list"])[1].get("employees") or []) if e.get("runtime") == "openclaw"]
        out = {}
        for ag in agents:
            groups = []
            for tgt in openclaw_adapter.load_channel_targets(ag):
                if not tgt.get("target_id"):
                    continue
                code_label = str(tgt.get("group_code") or "").strip()
                if not code_label:
                    key = str(tgt.get("key") or "")
                    segs = [s for s in key.split(":") if s and s not in {"line", "group", "store"}]
                    code_label = segs[0] if segs else key
                groups.append({
                    "group_code": code_label,
                    "target_kind": tgt.get("target_kind", "group"),
                    "target_id": tgt.get("target_id"),
                    "target_name": tgt.get("target_name", ""),
                    "channel": tgt.get("channel_type", "line"),
                    "active": bool(tgt.get("active", True)),
                })
            if groups:
                out[ag] = groups
        return HTTPStatus.OK, {"ok": True, "delivery_targets": out}
    if path == "/v1/tasks":
        argv = ["task", "list"]
        agent = query_value(query, "agent")
        status = query_value(query, "status")
        if agent:
            argv.extend(["--agent", agent])
        if status:
            argv.extend(["--status", status])
        code, payload = run_companyctl(argv)
        payload = sanitize_task_list_payload(payload)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/conversations"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/conversations").strip("/")
        code, payload = run_companyctl(["task", "conversations", "--task-id", task_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/attempts"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/attempts").strip("/")
        code, payload = run_companyctl(["task", "attempts", "--task-id", task_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/report"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/report").strip("/")
        code, payload = run_companyctl(["task", "report", "--task-id", task_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/reports/completed":
        argv = ["task", "report", "--limit", query_value(query, "limit", "40")]
        if truthy(query_value(query, "completed_only")):
            argv.append("--completed-only")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/"):
        task_id = path.removeprefix("/v1/tasks/").strip("/")
        code, payload = run_companyctl(["task", "show", "--task-id", task_id])
        payload = sanitize_task_detail_payload(payload)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/messages":
        agent = query_value(query, "agent")
        if not agent:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing query: agent"}
        code, payload = run_companyctl(["message", "list", "--agent", agent])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/messages/recent-direct":
        conn = companyctl.connect()
        try:
            limit_raw = query_value(query, "limit", "20")
            limit = int(limit_raw) if str(limit_raw).isdigit() else 20
            return HTTPStatus.OK, {"ok": True, "direct_messages_recent": company_dashboard.recent_direct_messages(conn, limit=limit)}
        finally:
            conn.close()
    if path == "/v1/events":
        limit_raw = query_value(query, "limit", "20")
        limit = int(limit_raw) if str(limit_raw).isdigit() else 20
        pending_only = truthy(query_value(query, "pending_only"))
        if pending_only:
            conn = companyctl.connect_readonly()
            try:
                events = companyctl.rows(
                    conn,
                    """
                    SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
                    FROM company_events
                    WHERE processed_at = ''
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 200)),),
                )
            finally:
                conn.close()
        else:
            events = list(reversed(recent_event_rows(limit=limit)))
        return HTTPStatus.OK, {"ok": True, "events": [sanitize_event_row(event) for event in events], "pending_only": pending_only}
    if path == "/v1/evidence":
        conn = companyctl.connect_readonly()
        try:
            task_id = query_value(query, "task_id")
            employee_id = query_value(query, "employee_id") or query_value(query, "employee")
            evidence = companyctl.audit_evidence_records(conn, task_id=task_id, employee_id=employee_id, limit=query_value(query, "limit", "50"))
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/evidence", "filters": {"task_id": task_id, "employee_id": employee_id}, "evidence": evidence}
    if path.startswith("/v1/evidence/") and (path.endswith("/content") or path.endswith("/safe-preview")):
        suffix = "/safe-preview" if path.endswith("/safe-preview") else "/content"
        evidence_id = path.removeprefix("/v1/evidence/").removesuffix(suffix).strip("/")
        conn = companyctl.connect_readonly()
        try:
            payload = companyctl.safe_evidence_content(conn, evidence_id)
        finally:
            conn.close()
        if payload.get("error") == "evidence not found":
            return HTTPStatus.NOT_FOUND, payload
        return (HTTPStatus.OK if payload.get("ok") else HTTPStatus.FORBIDDEN), payload
    if path == "/v1/artifacts":
        conn = companyctl.connect_readonly()
        try:
            artifacts = companyctl.audit_artifact_records(conn, task_id=query_value(query, "task_id"), limit=query_value(query, "limit", "50"))
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/artifacts", "artifacts": artifacts}
    if path == "/v1/runtime-sessions":
        conn = companyctl.connect_readonly()
        try:
            sessions = companyctl.list_runtime_sessions(
                conn,
                employee_id=query_value(query, "employee_id") or query_value(query, "employee"),
                task_id=query_value(query, "task_id"),
                trace_id=query_value(query, "trace_id"),
                limit=int(query_value(query, "limit", "50") or "50"),
            )
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/runtime-sessions", "runtime_sessions": sessions}
    if path.startswith("/v1/runtime-sessions/"):
        session_id = path.removeprefix("/v1/runtime-sessions/").strip("/")
        conn = companyctl.connect_readonly()
        try:
            payload = companyctl.runtime_session_detail_bundle(conn, session_id)
        finally:
            conn.close()
        if payload.get("error") == "runtime_session not found":
            return HTTPStatus.NOT_FOUND, payload
        if payload.get("error"):
            return HTTPStatus.BAD_REQUEST, payload
        return HTTPStatus.OK, payload
    if path == "/v1/tool-calls":
        conn = companyctl.connect_readonly()
        try:
            tool_calls = companyctl.list_tool_calls(
                conn,
                employee_id=query_value(query, "employee_id") or query_value(query, "employee"),
                task_id=query_value(query, "task_id"),
                trace_id=query_value(query, "trace_id"),
                attempt_id=query_value(query, "attempt_id"),
                session_id=query_value(query, "session_id"),
                limit=int(query_value(query, "limit", "50") or "50"),
            )
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/tool-calls", "tool_calls": tool_calls}
    if path.startswith("/v1/tool-calls/"):
        tool_call_id = path.removeprefix("/v1/tool-calls/").strip("/")
        conn = companyctl.connect_readonly()
        try:
            payload = companyctl.tool_call_detail_bundle(conn, tool_call_id)
        finally:
            conn.close()
        if payload.get("error") == "tool_call not found":
            return HTTPStatus.NOT_FOUND, payload
        if payload.get("error"):
            return HTTPStatus.BAD_REQUEST, payload
        return HTTPStatus.OK, payload
    if path == "/v1/budget-events":
        conn = companyctl.connect_readonly()
        try:
            budget_events = companyctl.list_budget_events(
                conn,
                employee_id=query_value(query, "employee_id") or query_value(query, "employee"),
                task_id=query_value(query, "task_id"),
                trace_id=query_value(query, "trace_id"),
                attempt_id=query_value(query, "attempt_id"),
                limit=int(query_value(query, "limit", "50") or "50"),
            )
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/budget-events", "budget_events": budget_events}
    if path == "/v1/budget-summary":
        conn = companyctl.connect_readonly()
        try:
            summary = companyctl.budget_summary(
                conn,
                employee_id=query_value(query, "employee_id") or query_value(query, "employee"),
                task_id=query_value(query, "task_id"),
                trace_id=query_value(query, "trace_id"),
                attempt_id=query_value(query, "attempt_id"),
            )
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/budget-summary", "summary": summary}
    if path == "/v1/economics":
        conn = companyctl.connect_readonly()
        try:
            economics = companyctl.compute_economics(conn)
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/economics", **economics}
    if path == "/v1/verifier-accuracy":
        conn = companyctl.connect_readonly()
        try:
            accuracy = companyctl.compute_verifier_accuracy(conn)
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/verifier-accuracy", **accuracy}
    if path == "/v1/handoffs":
        conn = companyctl.connect_readonly()
        try:
            handoffs = companyctl.audit_handoff_records(conn, task_id=query_value(query, "task_id"), limit=query_value(query, "limit", "50"))
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/handoffs", "handoffs": handoffs}
    if path == "/v1/failures":
        conn = companyctl.connect_readonly()
        try:
            failures = companyctl.audit_failure_records(conn, task_id=query_value(query, "task_id"), limit=query_value(query, "limit", "50"))
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "source": "/v1/failures", "failures": failures}
    if path.startswith("/v1/traces/") and path.endswith("/timeline"):
        trace_id = path.removeprefix("/v1/traces/").removesuffix("/timeline").strip("/")
        if not trace_id or "/" in trace_id:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}
        conn = companyctl.connect_readonly()
        try:
            trace = company_trace.load_trace(conn, trace_id)
            return HTTPStatus.OK, company_trace.safe_trace_payload(trace)
        finally:
            conn.close()
    if path.startswith("/v1/traces/") and path.endswith("/file-flow"):
        trace_id = path.removeprefix("/v1/traces/").removesuffix("/file-flow").strip("/")
        if not trace_id or "/" in trace_id:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}
        conn = companyctl.connect_readonly()
        try:
            return HTTPStatus.OK, companyctl.trace_file_flow_graph(conn, trace_id)
        finally:
            conn.close()
    if path == "/v1/workspaces/prune":
        if not truthy(query_value(query, "dry_run")):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "workspace prune API is dry-run only; pass dry_run=true"}
        limit_raw = query_value(query, "limit", "100")
        older_raw = query_value(query, "older_than_days", "30")
        limit = int(limit_raw) if str(limit_raw).isdigit() else 100
        older_than_days = int(older_raw) if str(older_raw).isdigit() else 30
        conn = companyctl.connect_readonly()
        try:
            return HTTPStatus.OK, companyctl.workspace_prune_preview(conn, older_than_days=older_than_days, limit=limit)
        finally:
            conn.close()
    if path == "/v1/dashboard/communication-observability":
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
            return HTTPStatus.OK, {"ok": True, **company_dashboard.communication_observability_summary(summary)}
        finally:
            conn.close()
    if path == "/v1/dashboard/cockpit":
        conn = companyctl.connect_readonly()
        try:
            summary = company_dashboard.load_summary(conn)
            employees = company_dashboard.employee_view_models(summary)
            doctor_code, doctor_payload = run_companyctl(["doctor", "--summary"])
            doctor = {
                "ok": bool(doctor_payload.get("ok")),
                "exit_code": doctor_code,
                "issue_count": len(doctor_payload.get("issues", []) or []),
                "issues": doctor_payload.get("issues", []) or [],
                "attention": doctor_payload.get("attention", []) or [],
                "attention_count": int(doctor_payload.get("attention_count") or len(doctor_payload.get("attention", []) or [])),
                "generated_at": companyctl.now(),
                "counts": doctor_payload.get("counts", {}),
                "heartbeat": doctor_payload.get("heartbeat", {}),
            }
            return HTTPStatus.OK, company_dashboard.build_cockpit_summary(
                {
                    **summary,
                    "employees": employees,
                    "all_employees": summary.get("employees", []),
                    "doctor": doctor,
                    "openclaw_native_status": companyctl.openclaw_native_status(),
                }
            )
        finally:
            conn.close()
    if path == "/v1/telemetry/traces":
        conn = companyctl.connect()
        try:
            limit_raw = query_value(query, "limit", "20")
            limit = int(limit_raw) if str(limit_raw).isdigit() else 20
            return HTTPStatus.OK, {"ok": True, "traces": company_dashboard.build_traces(conn, limit=limit)}
        finally:
            conn.close()
    if path == "/v1/progress/notifications":
        conn = companyctl.connect()
        try:
            limit_raw = query_value(query, "limit", "20")
            limit = int(limit_raw) if str(limit_raw).isdigit() else 20
            pending_only = truthy(query_value(query, "pending_only"))
            items = companyctl.list_progress_notifications(conn, pending_only=pending_only, limit=limit)
            total_items = companyctl.list_progress_notifications(conn, pending_only=False, limit=200)
            return HTTPStatus.OK, {
                "ok": True,
                "counts": {
                    "total": len(total_items),
                    "pending": sum(1 for item in items if item.get("pending")),
                    "sent": sum(1 for item in total_items if item.get("delivery_status") == "sent"),
                    "skipped": sum(1 for item in total_items if item.get("delivery_status") == "skipped"),
                    "failed": sum(1 for item in total_items if item.get("delivery_status") == "failed"),
                    "shown": len(items),
                },
                "items": items,
            }
        finally:
            conn.close()
    if path == "/v1/openclaw/runtime-inventory":
        conn = companyctl.connect_readonly()
        try:
            inventory = {"ok": True, **companyctl.openclaw_runtime_inventory(conn)}
            if truthy(query_value(query, "summary")):
                return HTTPStatus.OK, companyctl.openclaw_runtime_inventory_summary(inventory)
            return HTTPStatus.OK, inventory
        finally:
            conn.close()
    if path == "/v1/openclaw/native-status":
        status = companyctl.openclaw_native_status()
        if truthy(query_value(query, "summary")):
            return HTTPStatus.OK, companyctl.openclaw_native_status_summary(status)
        return HTTPStatus.OK, status
    if path == "/v1/agent-matrix":
        argv = ["agent-matrix"]
        agents = query_value(query, "agents")
        if agents:
            argv.extend(["--agents", agents])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/supervisor/delivery-loop":
        latest = companyctl.load_latest_supervisor_loop_result()
        return HTTPStatus.OK, {"ok": True, "latest_result": latest}
    if path == "/v1/dashboard/internal-watchdog":
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
            return HTTPStatus.OK, {"ok": True, "generated_at": summary.get("generated_at", ""), "internal_watchdog": summary.get("internal_watchdog", {})}
        finally:
            conn.close()
    if path == "/v1/followups":
        argv = ["followup", "list"]
        status = query_value(query, "status")
        if status:
            argv.extend(["--status", status])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/followups/"):
        followup_id = path.removeprefix("/v1/followups/").strip("/")
        code, payload = run_companyctl(["followup", "show", "--followup-id", followup_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/conversations":
        agent = query_value(query, "agent")
        if not agent:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing query: agent"}
        code, payload = run_companyctl(["conversation", "list", "--agent", agent])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/conversations/"):
        conversation_id = path.removeprefix("/v1/conversations/").strip("/")
        code, payload = run_companyctl(["conversation", "show", "--conversation-id", conversation_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/external-threads":
        conn = companyctl.connect()
        try:
            limit_raw = query_value(query, "limit", "50")
            limit = int(limit_raw) if str(limit_raw).isdigit() else 50
            return HTTPStatus.OK, {"ok": True, "threads": companyctl.list_external_threads(conn, platform=query_value(query, "platform"), owner_agent=query_value(query, "owner_agent"), limit=limit)}
        finally:
            conn.close()
    if path.startswith("/v1/external-threads/"):
        tail = path.removeprefix("/v1/external-threads/").strip("/")
        thread_id = tail.removesuffix("/messages").strip("/")
        conn = companyctl.connect()
        try:
            payload = companyctl.show_external_thread(conn, thread_id)
            if not payload.get("ok"):
                return HTTPStatus.NOT_FOUND, payload
            if tail.endswith("/messages"):
                return HTTPStatus.OK, {"ok": True, "thread_id": thread_id, "messages": payload["messages"]}
            return HTTPStatus.OK, payload
        finally:
            conn.close()
    if path == "/v1/approvals":
        argv = ["approval", "list"]
        status = query_value(query, "status", "all")
        agent = query_value(query, "agent")
        action = query_value(query, "action")
        limit = query_value(query, "limit")
        if status:
            argv.extend(["--status", status])
        if agent:
            argv.extend(["--agent", agent])
        if action:
            argv.extend(["--action", action])
        if limit:
            argv.extend(["--limit", limit])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/approvals/"):
        approval_id = path.removeprefix("/v1/approvals/").strip("/")
        conn = companyctl.connect()
        try:
            payload = companyctl.approval_detail_bundle(conn, approval_id)
        finally:
            conn.close()
        if payload.get("error") == "approval not found":
            return HTTPStatus.NOT_FOUND, payload
        return (HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_REQUEST), payload
    if path == "/v1/attendance/latest":
        latest_path = companyctl.STATE_DIR / "attendance" / "latest.json"
        if not latest_path.exists():
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "attendance latest report not found", "path": str(latest_path)}
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid attendance latest report: {exc}", "path": str(latest_path)}
        return HTTPStatus.OK, {"exit_code": 0, **payload}
    if path == "/v1/projects":
        argv = ["project", "list"]
        status = query_value(query, "status")
        if status:
            argv.extend(["--status", status])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/") and path.endswith("/review"):
        project_id = path.removeprefix("/v1/projects/").removesuffix("/review").strip("/")
        code, payload = run_companyctl(["project", "review", "--project-id", project_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/locks":
        argv = ["lock", "list"]
        agent = query_value(query, "agent")
        if agent:
            argv.extend(["--agent", agent])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/"):
        project_id = path.removeprefix("/v1/projects/").strip("/")
        code, payload = run_companyctl(["project", "show", "--project-id", project_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/adapter-runs":
        argv = ["runtime", "adapter-runs"]
        agent = query_value(query, "agent")
        status = query_value(query, "status")
        limit = query_value(query, "limit")
        if agent:
            argv.extend(["--agent", agent])
        if status:
            argv.extend(["--status", status])
        if query_value(query, "unacknowledged_only") in {"1", "true", "yes"}:
            argv.append("--unacknowledged-only")
        if limit:
            argv.extend(["--limit", limit])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/adapter-runs/"):
        run_id = path.removeprefix("/v1/adapter-runs/").strip("/")
        argv = ["runtime", "adapter-run", "show", "--run-id", run_id]
        if query_value(query, "summary") in {"1", "true", "yes"}:
            argv.append("--summary")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}


def employee_offboard_response(employee_id: str, body: dict) -> tuple[int, dict]:
    argv = ["employee", "offboard", "--id", employee_id]
    if truthy(body.get("hard_delete")):
        argv.append("--hard-delete")
    if truthy(body.get("dry_run")):
        argv.append("--dry-run")
    code, payload = run_companyctl(argv)
    return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}


def employee_profile_response(employee_id: str, body: dict) -> tuple[int, dict]:
    argv = ["employee", "update", "--id", employee_id]
    for key, flag in [
        ("name", "--name"),
        ("role", "--role"),
        ("runtime", "--runtime"),
        ("workspace", "--workspace"),
        ("status", "--status"),
        ("default_user_reply_channel", "--default-user-reply-channel"),
        ("default_user_reply_account", "--default-user-reply-account"),
        ("default_user_reply_to", "--default-user-reply-to"),
    ]:
        if body.get(key) not in {None, ""}:
            argv.extend([flag, str(body[key])])
    if body.get("default_user_reply_deliver") is not None:
        argv.append("--default-user-reply-deliver" if truthy(body.get("default_user_reply_deliver")) else "--no-default-user-reply-deliver")
    if truthy(body.get("dry_run")):
        argv.append("--dry-run")
    code, payload = run_companyctl(argv)
    return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}


def employee_communication_response(employee_id: str, body: dict) -> tuple[int, dict]:
    if body.get("enabled") is None:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing body: enabled"}
    result = companyctl.set_employee_communication_enabled(employee_id, truthy(body.get("enabled")), dry_run=truthy(body.get("dry_run")))
    return HTTPStatus.OK, result


def route_patch(path: str, body: dict) -> tuple[int, dict]:
    if path.startswith("/v1/employees/"):
        employee_id = path.removeprefix("/v1/employees/").strip("/")
        if not employee_id or "/" in employee_id:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}
        return employee_profile_response(employee_id, body)
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}


def route_delete(path: str, body: dict) -> tuple[int, dict]:
    if path.startswith("/v1/employees/"):
        employee_id = path.removeprefix("/v1/employees/").strip("/")
        if not employee_id or "/" in employee_id:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}
        return employee_offboard_response(employee_id, body)
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}


def route_post(path: str, body: dict) -> tuple[int, dict]:
    if path == "/v1/openclaw/dispatch-plan":
        result = companyctl.openclaw_native_dispatch_plan(
            source=str(body.get("source", "") or ""),
            target=str(body.get("target", "") or ""),
            task_type=str(body.get("type", body.get("task_type", "")) or ""),
            priority=str(body.get("priority", "P2") or "P2"),
            goal=str(body.get("goal", "") or ""),
            next_command=str(body.get("next_command", "") or ""),
            expected_evidence=str(body.get("expected_evidence", "") or ""),
            rollback=str(body.get("rollback", "") or ""),
            task_id=str(body.get("task_id", "") or ""),
        )
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST), result
    if path == "/v1/openclaw/dispatch-execute":
        result = companyctl.openclaw_native_dispatch_execute(
            source=str(body.get("source", "") or ""),
            target=str(body.get("target", "") or ""),
            task_type=str(body.get("type", body.get("task_type", "")) or ""),
            priority=str(body.get("priority", "P2") or "P2"),
            goal=str(body.get("goal", "") or ""),
            next_command=str(body.get("next_command", "") or ""),
            expected_evidence=str(body.get("expected_evidence", "") or ""),
            rollback=str(body.get("rollback", "") or ""),
            approval_id=str(body.get("approval_id", "") or ""),
            task_id=str(body.get("task_id", "") or ""),
        )
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST), result
    if path == "/v1/openclaw/import-results":
        result = companyctl.openclaw_native_import_results(
            limit=int(body.get("limit", 50) or 50),
            agent=str(body.get("agent", "") or ""),
        )
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST), result
    if path == "/v1/settings/notification":
        payload = companyctl.update_notification_settings(body)
        return (HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_REQUEST), payload
    if path == "/v1/approval/mode":
        mode = str(body.get("mode", "") or "")
        if mode not in companyctl.ROUTE_APPROVAL_MODES:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"mode must be one of {sorted(companyctl.ROUTE_APPROVAL_MODES)}", "got": mode}
        code, payload = run_companyctl(["approval", "mode", "--set", mode, "--by", str(body.get("by", "owner-shift") or "owner-shift")])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path == "/v1/memory/projects":  # create a project
        code, payload = run_companyctl(["memory", "project", "create",
            "--id", str(body.get("id", "") or ""), "--name", str(body.get("name", "") or ""),
            "--workspace", str(body.get("workspace", "") or ""), "--lead", str(body.get("lead", "hermes") or "hermes")])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path.startswith("/v1/memory/projects/") and path.endswith("/executors"):  # lock who may work it
        pid = path.removeprefix("/v1/memory/projects/").removesuffix("/executors").strip("/")
        execs = body.get("executors", [])
        execs_csv = ",".join(str(x) for x in execs) if isinstance(execs, list) else str(execs or "")
        code, payload = run_companyctl(["memory", "project", "set-executors", "--id", pid, "--executors", execs_csv])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path.startswith("/v1/memory/projects/") and path.endswith("/remember"):
        pid = path.removeprefix("/v1/memory/projects/").removesuffix("/remember").strip("/")
        code, payload = run_companyctl(["memory", "remember", "--project", pid,
            "--title", str(body.get("title", "") or ""), "--body", str(body.get("body", "") or ""),
            "--type", str(body.get("type", "fact") or "fact"), "--by", str(body.get("by", "owner-shift") or "owner-shift"),
            "--importance", str(int(body.get("importance", 2) or 2))])
        if code == 0:
            run_companyctl(["memory", "curate", "--project", pid, "--by", "owner-shift"])  # reflect it in the digest now
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path.startswith("/v1/memory/projects/") and path.endswith("/curate"):
        pid = path.removeprefix("/v1/memory/projects/").removesuffix("/curate").strip("/")
        code, payload = run_companyctl(["memory", "curate", "--project", pid, "--by", str(body.get("by", "owner-shift") or "owner-shift")])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), payload
    if path.startswith("/v1/memory/entries/") and path.endswith("/archive"):
        eid = path.removeprefix("/v1/memory/entries/").removesuffix("/archive").strip("/")
        code, payload = run_companyctl(["memory", "archive", "--entry-id", eid, "--by", str(body.get("by", "owner-shift") or "owner-shift")])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.NOT_FOUND), payload
    if path == "/v1/notifications/send":
        result = companyctl.notification_send_result(
            message=str(body.get("message", "")),
            kind=str(body.get("kind", "general") or "general"),
            subject=str(body.get("subject", "") or ""),
            target=str(body.get("target", "") or ""),
            account_id=str(body.get("account", "") or ""),
            dry_run=truthy(body.get("dry_run")),
        )
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST), result
    if path.startswith("/v1/evidence/") and (path.endswith("/accept") or path.endswith("/reject")):
        status_value = "accepted" if path.endswith("/accept") else "rejected"
        suffix = "/accept" if status_value == "accepted" else "/reject"
        evidence_id = path.removeprefix("/v1/evidence/").removesuffix(suffix).strip("/")
        conn = companyctl.connect()
        try:
            result = companyctl.decide_evidence_internal(
                conn,
                evidence_id=evidence_id,
                by=str(body.get("by", "")),
                status=status_value,
                summary=str(body.get("summary", "")),
                reason=str(body.get("reason", "")),
            )
        finally:
            conn.close()
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST), result
    if path == "/v1/policy-blocks/report":
        argv = [
            "policy",
            "block-report",
            "--error",
            str(body.get("error", "")),
        ]
        for key, flag in [("source", "--source"), ("target", "--target"), ("tool", "--tool"), ("operation", "--operation"), ("block_id", "--block-id")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        if truthy(body.get("dry_run")):
            argv.append("--dry-run")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/budget-events":
        conn = companyctl.connect()
        try:
            payload = companyctl.record_budget_event_internal(
                conn,
                budget_event_id=str(body.get("budget_event_id", "") or ""),
                budget_account_id=str(body.get("budget_account_id", "") or ""),
                trace_id=str(body.get("trace_id", "") or ""),
                task_id=str(body.get("task_id", "") or ""),
                attempt_id=str(body.get("attempt_id", "") or ""),
                employee_id=str(body.get("employee_id", body.get("employee", "")) or ""),
                cost_type=str(body.get("cost_type", "") or ""),
                amount=float(body.get("amount", 0) or 0),
                currency=str(body.get("currency", "USD") or "USD"),
                token_input=int(body.get("token_input", 0) or 0),
                token_output=int(body.get("token_output", 0) or 0),
                model_name=str(body.get("model_name", "") or ""),
                provider=str(body.get("provider", "") or ""),
                runtime_seconds=int(body.get("runtime_seconds", 0) or 0),
                summary=str(body.get("summary", "") or ""),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            )
        except (SystemExit, ValueError) as exc:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        finally:
            conn.close()
        return HTTPStatus.CREATED, {"ok": True, "source": "/v1/budget-events", **payload}
    if path == "/v1/supervisor/delivery-loop":
        argv = ["supervisor", "delivery-loop"]
        if body.get("limit") not in {None, ""}:
            argv.extend(["--limit", str(body["limit"])])
        if body.get("by"):
            argv.extend(["--by", str(body["by"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload, "latest_result": payload}
    if path == "/v1/external-mirror/import":
        conn = companyctl.connect()
        try:
            result = companyctl.import_external_mirror(conn, body)
            return (HTTPStatus.CREATED if result.get("ok") else HTTPStatus.BAD_REQUEST), result
        finally:
            conn.close()
    if path == "/v1/dashboard/internal-watchdog/remediate":
        conn = companyctl.connect()
        try:
            result = company_dashboard.remediate_internal_watchdog(
                conn,
                source_agent=str(body.get("source", "main") or "main"),
                dry_run=truthy(body.get("dry_run", True)),
                deliver=truthy(body.get("deliver")),
                escalate_to=str(body.get("escalate_to", "hermes") or "hermes"),
                escalate_existing=truthy(body.get("escalate_existing", True)),
                reroute_to=str(body.get("reroute_to", "codex") or "codex"),
                create_reroute_plan=truthy(body.get("create_reroute_plan", True)),
            )
            return HTTPStatus.OK, result
        finally:
            conn.close()
    if path == "/v1/dashboard/internal-watchdog/apply-reroutes":
        conn = companyctl.connect()
        try:
            result = company_dashboard.apply_reroute_decisions(
                conn,
                by=str(body.get("by", "hermes") or "hermes"),
                dry_run=truthy(body.get("dry_run", True)),
            )
            return HTTPStatus.OK, result
        finally:
            conn.close()
    if path == "/v1/employees/onboard":
        argv = [
            "employee",
            "onboard",
            "--id",
            str(body.get("id", "")),
            "--name",
            str(body.get("name", "")),
            "--role",
            str(body.get("role", "")),
            "--runtime",
            str(body.get("runtime", "")),
            "--workspace",
            str(body.get("workspace", "")),
        ]
        for key, flag in [
            ("alias", "--alias"),
            ("skills", "--skills"),
            ("tools", "--tools"),
            ("task_types", "--task-types"),
            ("can_talk_to", "--can-talk-to"),
            ("can_assign_to", "--can-assign-to"),
            ("channel", "--channel"),
            ("handoff_mode", "--handoff-mode"),
            ("default_user_reply_channel", "--default-user-reply-channel"),
            ("default_user_reply_account", "--default-user-reply-account"),
            ("default_user_reply_to", "--default-user-reply-to"),
            ("requires_approval_for", "--requires-approval-for"),
            ("test_source", "--test-source"),
            ("test_task_id", "--test-task-id"),
        ]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        if truthy(body.get("default_user_reply_deliver")):
            argv.append("--default-user-reply-deliver")
        for key, flag in [
            ("open_communication", "--open-communication"),
            ("no_submit_tasks", "--no-submit-tasks"),
            ("no_claim_tasks", "--no-claim-tasks"),
            ("can_modify_kernel", "--can-modify-kernel"),
            ("create_test_task", "--create-test-task"),
            ("dry_run", "--dry-run"),
        ]:
            if truthy(body.get(key)):
                argv.append(flag)
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/employees":
        code, payload = run_companyctl(
            [
                "employee",
                "create",
                "--id",
                str(body.get("id", "")),
                "--name",
                str(body.get("name", "")),
                "--role",
                str(body.get("role", "")),
                "--runtime",
                str(body.get("runtime", "")),
                "--workspace",
                str(body.get("workspace", "")),
            ]
        )
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/employees/") and path.endswith("/verify-runtime"):
        # Verifying invokes the runtime (can take up to its timeout), so run it detached and let the
        # console poll /v1/employees for the status flip to 'active'. Closes the self-serve onboarding
        # loop: form creates a candidate → click verify → employee activates once its runtime responds.
        employee_id = re.sub(r"[^A-Za-z0-9_.-]", "", path.removeprefix("/v1/employees/").removesuffix("/verify-runtime").strip("/"))
        if not employee_id:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid employee id"}
        argv = ["employee", "verify-runtime", "--id", employee_id, "--activate"]
        if body.get("from"):
            argv.extend(["--from", str(body["from"])])
        if body.get("timeout") not in {None, ""}:
            argv.extend(["--timeout", str(int(body["timeout"]))])
        ok, info = spawn_companyctl_detached(argv, companyctl.ROOT / "logs" / "verify-runtime" / f"{employee_id}.log")
        return (HTTPStatus.ACCEPTED if ok else HTTPStatus.BAD_REQUEST), info
    if path.startswith("/v1/employees/") and path.endswith("/offboard"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/offboard").strip("/")
        return employee_offboard_response(employee_id, body)
    if path.startswith("/v1/employees/") and path.endswith("/profile"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/profile").strip("/")
        return employee_profile_response(employee_id, body)
    if path.startswith("/v1/employees/") and path.endswith("/communication"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/communication").strip("/")
        return employee_communication_response(employee_id, body)
    if path == "/v1/runtimes":
        argv = ["runtime", "register", "--runtime", str(body.get("runtime", ""))]
        for key, flag in [("command", "--command"), ("status", "--status"), ("notes", "--notes")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/employees/") and path.endswith("/capabilities"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/capabilities").strip("/")
        argv = ["employee", "capabilities", "--id", employee_id]
        for key, flag in [("set_skills", "--set-skills"), ("set_tools", "--set-tools"), ("set_task_types", "--set-task-types")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        for value in body_values(body, "add_skill"):
            argv.extend(["--add-skill", value])
        for value in body_values(body, "add_tool"):
            argv.extend(["--add-tool", value])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/employees/") and path.endswith("/permissions"):
        employee_id = path.removeprefix("/v1/employees/").removesuffix("/permissions").strip("/")
        argv = ["employee", "permissions", "--id", employee_id]
        for key, flag in [
            ("can_submit_tasks", "--can-submit-tasks"),
            ("can_claim_tasks", "--can-claim-tasks"),
            ("can_modify_kernel", "--can-modify-kernel"),
            ("requires_approval_for", "--requires-approval-for"),
        ]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key]).lower()])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/employees/match":
        argv = ["employee", "match"]
        for key, flag in [
            ("skills", "--skills"),
            ("tools", "--tools"),
            ("task_type", "--task-type"),
            ("runtime", "--runtime"),
            ("role", "--role"),
            ("limit", "--limit"),
        ]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        if truthy(body.get("include_unavailable")):
            argv.append("--include-unavailable")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/tasks/route":
        argv = [
            "task",
            "route",
            "--from",
            str(body.get("from", "")),
            "--title",
            str(body.get("title", "")),
            "--description",
            str(body.get("description", "")),
        ]
        for key, flag in [
            ("priority", "--priority"),
            ("task_id", "--task-id"),
            ("skills", "--skills"),
            ("tools", "--tools"),
            ("task_type", "--task-type"),
            ("runtime", "--runtime"),
            ("role", "--role"),
            ("limit", "--limit"),
            ("requires_approval", "--requires-approval"),
            ("approval_id", "--approval-id"),
            ("risk", "--risk"),
            ("changed_files", "--changed-files"),
            ("rfc", "--rfc"),
        ]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        if truthy(body.get("include_unavailable")):
            argv.append("--include-unavailable")
        code, payload = run_companyctl(argv)
        if code == 0:
            return HTTPStatus.CREATED, {"exit_code": code, **payload}
        if code == 2 and payload.get("error") == "approval required":
            return HTTPStatus.ACCEPTED, {"exit_code": code, **payload}
        return HTTPStatus.BAD_REQUEST, {"exit_code": code, **payload}
    if path == "/v1/tasks":
        argv = [
            "task",
            "submit",
            "--from",
            str(body.get("from", "")),
            "--to",
            str(body.get("to", "")),
            "--title",
            str(body.get("title", "")),
            "--description",
            str(body.get("description", "")),
        ]
        if body.get("task_id"):
            argv.extend(["--task-id", str(body["task_id"])])
        if body.get("priority"):
            argv.extend(["--priority", str(body["priority"])])
        if body.get("requires_approval"):
            argv.extend(["--requires-approval", str(body["requires_approval"])])
        if body.get("approval_id"):
            argv.extend(["--approval-id", str(body["approval_id"])])
        deliver_to = body.get("deliver_to")
        if isinstance(deliver_to, dict):
            argv.extend(["--deliver-to", json.dumps(deliver_to, ensure_ascii=False)])
        elif isinstance(deliver_to, str) and deliver_to.strip():
            argv.extend(["--deliver-to", deliver_to.strip()])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/messages/direct":
        argv = ["message", "direct", "--from", str(body.get("from", "")), "--to", str(body.get("to", "")), "--body", str(body.get("body", ""))]
        for key, flag in [
            ("message_id", "--message-id"),
            ("session_key", "--session-key"),
            ("timeout", "--timeout"),
            ("reply_channel", "--reply-channel"),
            ("reply_to", "--reply-to"),
            ("reply_account", "--reply-account"),
        ]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        if truthy(body.get("deliver")):
            argv.append("--deliver")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/followups":
        argv = ["followup", "request", "--from", str(body.get("from", "")), "--to", str(body.get("to", "")), "--question", str(body.get("question", ""))]
        for key, flag in [("context", "--context"), ("followup_id", "--followup-id"), ("message_id", "--message-id"), ("session_key", "--session-key"), ("timeout", "--timeout"), ("reply_channel", "--reply-channel"), ("reply_account", "--reply-account"), ("reply_to", "--reply-to")]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        if truthy(body.get("deliver")):
            argv.append("--deliver")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/followups/") and path.endswith("/reply"):
        followup_id = path.removeprefix("/v1/followups/").removesuffix("/reply").strip("/")
        argv = ["followup", "reply", "--followup-id", followup_id, "--by", str(body.get("by", "")), "--answer", str(body.get("answer", ""))]
        if body.get("message_id"):
            argv.extend(["--message-id", str(body["message_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/messages":
        argv = ["message", "send", "--from", str(body.get("from", "")), "--to", str(body.get("to", "")), "--body", str(body.get("body", ""))]
        if body.get("message_id"):
            argv.extend(["--message-id", str(body["message_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/messages/channel-send":
        argv = ["message", "channel-send", "--agent", str(body.get("agent", "")), "--channel", str(body.get("channel", "line")), "--body", str(body.get("body", "")), "--by", str(body.get("by", "owner-shift"))]
        if body.get("group_code"):
            argv.extend(["--group-code", str(body["group_code"])])
        if body.get("target_id"):
            argv.extend(["--target-id", str(body["target_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 and payload.get("ok") else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/conversations"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/conversations").strip("/")
        argv = ["task", "discuss", "--task-id", task_id]
        for key, flag in [("from", "--from"), ("participants", "--participants"), ("title", "--title"), ("body", "--body"), ("conversation_id", "--conversation-id"), ("evidence", "--evidence")]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/run"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/run").strip("/")
        argv = ["task", "run", "--task-id", task_id, "--agent", str(body.get("agent", "")), "--by", str(body.get("by", ""))]
        for key, flag in [
            ("adapter_type", "--adapter-type"),
            ("pid", "--pid"),
            ("session_key", "--session-key"),
            ("max_runtime_seconds", "--max-runtime-seconds"),
            ("heartbeat_interval_seconds", "--heartbeat-interval-seconds"),
            ("progress_interval_seconds", "--progress-interval-seconds"),
            ("stale_after_seconds", "--stale-after-seconds"),
            ("supervisor_check_interval_seconds", "--supervisor-check-interval-seconds"),
            ("max_corrections", "--max-corrections"),
            ("max_retries", "--max-retries"),
        ]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/correct"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/correct").strip("/")
        if not truthy(body.get("ack")) and not truthy(body.get("execute")):
            return control_action_approval_response(
                task_id=task_id,
                action="correction",
                by=str(body.get("by", "")),
                reason=str(body.get("message", "")),
                attempt_id=str(body.get("attempt_id", "")),
                risk="P1",
            )
        argv = ["task", "correct", "--task-id", task_id, "--attempt-id", str(body.get("attempt_id", "")), "--by", str(body.get("by", "")), "--message", str(body.get("message", ""))]
        if truthy(body.get("ack")):
            argv.append("--ack")
        code, payload = run_companyctl(argv)
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(
                response,
                action="correction_ack" if truthy(body.get("ack")) else "correction",
                event_type="supervisor.correction_acknowledged" if truthy(body.get("ack")) else "supervisor.correction_requested",
                requires_owner_approval=not truthy(body.get("ack")),
            )
            if truthy(body.get("execute")) and not truthy(body.get("ack")):
                response["executed"] = True
                response["control_action"]["approval_mode"] = "owner_approved_execute"
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path.startswith("/v1/tasks/") and path.endswith("/progress"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/progress").strip("/")
        argv = ["task", "progress", "--task-id", task_id, "--agent", str(body.get("agent", "")), "--message", str(body.get("message", ""))]
        for key, flag in [("attempt_id", "--attempt-id"), ("state", "--state"), ("progress", "--progress"), ("at", "--at")]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        payload_body = body.get("payload")
        if payload_body is not None and payload_body != "":
            argv.extend(["--payload", json.dumps(payload_body, ensure_ascii=False) if isinstance(payload_body, dict) else str(payload_body)])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/probe"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/probe").strip("/")
        argv = ["task", "probe", "--task-id", task_id, "--by", str(body.get("by", "")), "--message", str(body.get("message", ""))]
        for key, flag in [("attempt_id", "--attempt-id"), ("reason", "--reason")]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(
                response,
                action="task.probe",
                event_type="task.probe",
                requires_owner_approval=False,
            )
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path.startswith("/v1/tasks/") and path.endswith("/cancel"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/cancel").strip("/")
        if not truthy(body.get("execute")):
            return control_action_approval_response(
                task_id=task_id,
                action="cancel",
                by=str(body.get("by", "")),
                reason=str(body.get("reason", "")),
                attempt_id=str(body.get("attempt_id", "")),
                risk="P0",
                dangerous=True,
            )
        code, payload = run_companyctl(["task", "cancel", "--task-id", task_id, "--attempt-id", str(body.get("attempt_id", "")), "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(response, action="cancel", event_type="supervisor.cancel_requested", dangerous=True)
            response["executed"] = True
            response["control_action"]["approval_mode"] = "owner_approved_execute"
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path.startswith("/v1/tasks/") and path.endswith("/claim"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/claim").strip("/")
        argv = ["task", "claim", "--agent", str(body.get("agent", "")), "--task-id", task_id]
        if body.get("lease_seconds"):
            argv.extend(["--lease-seconds", str(body["lease_seconds"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/done"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/done").strip("/")
        code, payload = run_companyctl(["task", "done", "--agent", str(body.get("agent", "")), "--task-id", task_id, "--summary", str(body.get("summary", "")), "--evidence", str(body.get("evidence", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/block"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/block").strip("/")
        code, payload = run_companyctl(["task", "block", "--agent", str(body.get("agent", "")), "--task-id", task_id, "--blocker", str(body.get("blocker", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/reopen"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/reopen").strip("/")
        argv = ["task", "reopen", "--task-id", task_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))]
        if body.get("status"):
            argv.extend(["--status", str(body["status"])])
        if body.get("description"):
            argv.extend(["--description", str(body["description"])])
        code, payload = run_companyctl(argv)
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(response, action="reopen", event_type="task.reopened")
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path.startswith("/v1/tasks/") and path.endswith("/discard"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/discard").strip("/")
        code, payload = run_companyctl(["task", "discard", "--task-id", task_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", "owner discarded"))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/retry"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/retry").strip("/")
        if not truthy(body.get("execute")):
            return control_action_approval_response(
                task_id=task_id,
                action="retry",
                by=str(body.get("by", "")),
                reason=str(body.get("reason", "")),
                risk="P1",
            )
        code, payload = run_companyctl(["task", "retry", "--task-id", task_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(response, action="retry", event_type="task.retrying")
            response["executed"] = True
            response["control_action"]["approval_mode"] = "owner_approved_execute"
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path.startswith("/v1/tasks/") and path.endswith("/reassign"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/reassign").strip("/")
        if not truthy(body.get("execute")):
            return control_action_approval_response(
                task_id=task_id,
                action="reassign",
                by=str(body.get("by", "")),
                reason=str(body.get("reason", "")),
                target=str(body.get("to", "")),
                risk="P1",
            )
        code, payload = run_companyctl(["task", "reassign", "--task-id", task_id, "--by", str(body.get("by", "")), "--to", str(body.get("to", "")), "--reason", str(body.get("reason", ""))])
        response = {"exit_code": code, **payload}
        if code == 0:
            response = with_control_action(response, action="reassign", event_type="task.reassigned")
            response["executed"] = True
            response["control_action"]["approval_mode"] = "owner_approved_execute"
            response = attach_task_control_context(response, task_id)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), response
    if path == "/v1/conversations":
        argv = [
            "conversation",
            "start",
            "--from",
            str(body.get("from", "")),
            "--participants",
            str(body.get("participants", "")),
            "--title",
            str(body.get("title", "")),
            "--body",
            str(body.get("body", "")),
        ]
        if body.get("evidence"):
            argv.extend(["--evidence", str(body["evidence"])])
        if body.get("conversation_id"):
            argv.extend(["--conversation-id", str(body["conversation_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/conversations/") and path.endswith("/join"):
        conversation_id = path.removeprefix("/v1/conversations/").removesuffix("/join").strip("/")
        code, payload = run_companyctl(
            [
                "conversation",
                "join",
                "--agent",
                str(body.get("agent", "owner-shift") or "owner-shift"),
                "--conversation-id",
                conversation_id,
            ]
        )
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/conversations/probe":
        # Probing every employee invokes each runtime in turn (minutes). Running it in-process
        # would hold the CLI lock and freeze the gateway, so spawn it detached; it persists the
        # allowlist to state/meeting-capable.json which the meeting gate reads.
        argv = ["conversation", "probe", "--participants", str(body.get("participants", "active") or "active")]
        if body.get("timeout") not in {None, ""}:
            argv.extend(["--timeout", str(int(body["timeout"]))])
        ok, info = spawn_companyctl_detached(argv, companyctl.ROOT / "logs" / "conversation-run" / "probe.log")
        return (HTTPStatus.ACCEPTED if ok else HTTPStatus.BAD_REQUEST), info
    if path.startswith("/v1/conversations/") and path.endswith("/run"):
        conversation_id = path.removeprefix("/v1/conversations/").removesuffix("/run").strip("/")
        # A discussion invokes several runtimes in turn and can take minutes. Running it
        # in-process would hold the global CLI lock and freeze the whole console, so we
        # spawn it detached and let the client poll the thread for progressive results
        # (each turn commits its message as it lands).
        ok, info = spawn_conversation_run(conversation_id, body)
        return (HTTPStatus.ACCEPTED if ok else HTTPStatus.BAD_REQUEST), info
    if path.startswith("/v1/conversations/") and path.endswith("/reply"):
        conversation_id = path.removeprefix("/v1/conversations/").removesuffix("/reply").strip("/")
        argv = [
            "conversation",
            "reply",
            "--from",
            str(body.get("from", "")),
            "--conversation-id",
            conversation_id,
            "--body",
            str(body.get("body", "")),
        ]
        if body.get("evidence"):
            argv.extend(["--evidence", str(body["evidence"])])
        if body.get("message_id"):
            argv.extend(["--message-id", str(body["message_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/heartbeats":
        code, payload = run_companyctl(["heartbeat", "--agent", str(body.get("agent", ""))])
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/attendance/sweep":
        argv = ["attendance", "sweep", "--source", str(body.get("source", "main"))]
        for key, flag in [("agents", "--agents"), ("sweep_id", "--sweep-id"), ("stale_minutes", "--stale-minutes"), ("reply_timeout", "--reply-timeout")]:
            if body.get(key) not in {None, ""}:
                argv.extend([flag, str(body[key])])
        if truthy(body.get("include_candidates")):
            argv.append("--include-candidates")
        if body.get("probe_replies") is not None and not truthy(body.get("probe_replies")):
            argv.append("--no-probe-replies")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.ACCEPTED), {"exit_code": code, **payload}
    if path == "/v1/locks/acquire":
        argv = ["lock", "acquire", "--agent", str(body.get("agent", "")), "--resource", str(body.get("resource", ""))]
        if body.get("lease_seconds"):
            argv.extend(["--lease-seconds", str(body["lease_seconds"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/locks/release":
        argv = ["lock", "release", "--agent", str(body.get("agent", "")), "--resource", str(body.get("resource", ""))]
        if body.get("force") in {True, "1", "true", "yes"}:
            argv.append("--force")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/locks/unlock-stale":
        code, payload = run_companyctl(["lock", "unlock-stale"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/projects":
        argv = [
            "project",
            "create",
            "--title",
            str(body.get("title", "")),
            "--owner",
            str(body.get("owner", "")),
        ]
        for key, flag in [("project_id", "--project-id"), ("goal", "--goal"), ("status", "--status"), ("acceptance", "--acceptance")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/") and path.endswith("/tasks"):
        project_id = path.removeprefix("/v1/projects/").removesuffix("/tasks").strip("/")
        code, payload = run_companyctl(["project", "link-task", "--project-id", project_id, "--task-id", str(body.get("task_id", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/") and path.endswith("/plan-items"):
        project_id = path.removeprefix("/v1/projects/").removesuffix("/plan-items").strip("/")
        argv = ["project", "plan-add", "--project-id", project_id, "--title", str(body.get("title", ""))]
        for key, flag in [("status", "--status"), ("owner", "--owner"), ("due_at", "--due-at"), ("task_id", "--task-id"), ("plan_id", "--plan-id")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/") and path.endswith("/status"):
        rest = path.removeprefix("/v1/projects/").removesuffix("/status").strip("/")
        if "/plan-items/" in rest:
            project_id, plan_id = rest.split("/plan-items/", 1)
            code, payload = run_companyctl(["project", "plan-status", "--project-id", project_id.strip("/"), "--plan-id", plan_id.strip("/"), "--status", str(body.get("status", ""))])
            return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
        code, payload = run_companyctl(["project", "status", "--project-id", rest.strip("/"), "--status", str(body.get("status", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/projects/") and path.endswith("/accept"):
        project_id = path.removeprefix("/v1/projects/").removesuffix("/accept").strip("/")
        argv = ["project", "accept", "--project-id", project_id, "--by", str(body.get("by", "")), "--summary", str(body.get("summary", ""))]
        if body.get("force") in {True, "1", "true", "yes"}:
            argv.append("--force")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/approvals":
        argv = [
            "approval",
            "request",
            "--from",
            str(body.get("from", "")),
            "--action",
            str(body.get("action", "")),
            "--reason",
            str(body.get("reason", "")),
        ]
        for key, flag in [("target", "--target"), ("risk", "--risk"), ("evidence", "--evidence"), ("approval_id", "--approval-id"), ("task_id", "--task-id")]:
            if body.get(key):
                argv.extend([flag, str(body[key])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/approvals/") and path.endswith("/approve"):
        approval_id = path.removeprefix("/v1/approvals/").removesuffix("/approve").strip("/")
        code, payload = run_companyctl(["approval", "approve", "--approval-id", approval_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/approvals/") and path.endswith("/deny"):
        approval_id = path.removeprefix("/v1/approvals/").removesuffix("/deny").strip("/")
        code, payload = run_companyctl(["approval", "deny", "--approval-id", approval_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/approvals/") and path.endswith("/resolve"):
        approval_id = path.removeprefix("/v1/approvals/").removesuffix("/resolve").strip("/")
        argv = ["approval", "resolve", "--approval-id", approval_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", "")), "--mock"]
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/adapter-runs/") and path.endswith("/ack"):
        run_id = path.removeprefix("/v1/adapter-runs/").removesuffix("/ack").strip("/")
        code, payload = run_companyctl(["runtime", "ack-adapter-run", "--run-id", run_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/adapter-runs/") and path.endswith("/retry"):
        run_id = path.removeprefix("/v1/adapter-runs/").removesuffix("/retry").strip("/")
        argv = ["runtime", "retry-adapter-run", "--run-id", run_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))]
        if body.get("task_id"):
            argv.extend(["--task-id", str(body["task_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": path}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CompanyKernelAPI/0.1"

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome local-network access: allow file:// console to reach 127.0.0.1
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_sse_event(self, event: str, payload: dict, *, event_id: str = "") -> None:
        if event_id:
            self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        data = json.dumps(payload, ensure_ascii=False)
        for line in data.splitlines() or ["{}"]:
            self.wfile.write(f"data: {line}\n".encode("utf-8"))
        self.wfile.write(b"\n")
        self.wfile.flush()

    def stream_events(self, query: dict[str, list[str]]) -> None:
        limit_raw = query_value(query, "limit", "20")
        poll_raw = query_value(query, "poll_seconds", "2")
        cycles_raw = query_value(query, "max_cycles", "30")
        limit = int(limit_raw) if str(limit_raw).isdigit() else 20
        poll_seconds = max(1, min(int(poll_raw) if str(poll_raw).isdigit() else 2, 10))
        max_cycles = max(1, min(int(cycles_raw) if str(cycles_raw).isdigit() else 30, 300))
        self.send_response(HTTPStatus.OK)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_created_at = ""
        last_id = str(self.headers.get("Last-Event-ID", "") or "")
        self.send_sse_event(
            "stream_status",
            {
                "ok": True,
                "mode": "sqlite_short_poll",
                "poll_seconds": poll_seconds,
                "timeout_is_sync_wait_only": True,
                "timeout_semantics": "sync_wait_window",
                "failure_semantics": "task_failure_decided_by_attempt_evidence",
                "ledger_consistency": {
                    "source": "single_company_kernel_ledger",
                    "surfaces": ["api", "cli", "dashboard"],
                },
            },
        )
        for _ in range(max_cycles):
            try:
                events = recent_event_rows(limit=limit, after_created_at=last_created_at, after_id=last_id)
                for event in events:
                    safe_event = sanitize_event_row(event)
                    self.send_sse_event("company_event", safe_event, event_id=safe_event.get("id", ""))
                    last_created_at = event.get("created_at", last_created_at)
                    last_id = event.get("id", last_id)
                if not events:
                    self.send_sse_event("heartbeat", {"ok": True, "created_at": companyctl.now()})
                time.sleep(poll_seconds)
            except (BrokenPipeError, ConnectionResetError):
                break

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid json: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("json body must be an object")
        return payload

    def handle_cli(self, argv: list[str], *, ok_status: int = HTTPStatus.OK) -> None:
        try:
            code, payload = run_companyctl(argv)
        except SystemExit as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        status = ok_status if code == 0 else HTTPStatus.BAD_REQUEST
        self.send_json(status, {"exit_code": code, **payload})

    def send_html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def require_auth(self, method: str = "") -> bool:
        """Authenticate + authorize by role. 401 if the bearer is unknown, 403 if the actor's role
        is below what the action needs. Backward compatible: single-token / open mode → owner."""
        user, role = resolve_actor(self.headers)
        if not role:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized: missing or invalid Bearer token"})
            return False
        parsed = urlparse(self.path)
        needed = required_role(method or getattr(self, "command", "GET"), parsed.path)
        if ROLE_RANK.get(role, 0) < ROLE_RANK.get(needed, 0):
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": f"forbidden: '{needed}' role required, you are '{role}'", "user": user, "role": role})
            return False
        self.actor_user, self.actor_role = user, role
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in CONSOLE_PATHS:
            if CONSOLE_TEMPLATE.exists():
                self.send_html(HTTPStatus.OK, CONSOLE_TEMPLATE.read_text(encoding="utf-8"))
            else:
                self.send_html(HTTPStatus.NOT_FOUND, "<h1>console template missing</h1><p>expected at dashboard_templates/console.html</p>")
            return
        if not self.require_auth("GET"):
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/v1/events/stream":
            self.stream_events(query)
            return
        try:
            status, payload = route_get(unquote(parsed.path), query)
        except Exception as exc:
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": companyctl.sanitize_log_text(exc),
                "path": parsed.path,
            }
        self.send_json(status, payload)

    def do_POST(self) -> None:
        if not self.require_auth("POST"):
            return
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_post(unquote(parsed.path), body)
        except SystemExit as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        self.send_json(status, payload)

    def do_PATCH(self) -> None:
        if not self.require_auth("PATCH"):
            return
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_patch(unquote(parsed.path), body)
        except SystemExit as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        self.send_json(status, payload)

    def do_DELETE(self) -> None:
        if not self.require_auth("DELETE"):
            return
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_delete(unquote(parsed.path), body)
        except SystemExit as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        self.send_json(status, payload)

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)


def run_server(host: str, port: int, quiet: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.quiet = quiet
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel REST API Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_server(args.host, args.port, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
