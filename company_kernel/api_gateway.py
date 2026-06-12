from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import companyctl
from . import company_dashboard


CONSOLE_TEMPLATE = Path(
    os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])
).resolve() / "dashboard_templates" / "console.html"
CONSOLE_PATHS = {"/", "/console", "/ui", "/index.html"}


API_VERSION = "v1"
API_CAPABILITIES = [
    "health",
    "doctor",
    "tasks",
    "messages",
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
    "settings",
    "external_mirror",
]
API_ENDPOINTS = [
    {"method": "GET", "path": "/v1/health", "summary": "Company Kernel health summary"},
    {"method": "GET", "path": "/v1/doctor", "summary": "Doctor summary", "query": {"strict_launchd": "bool optional", "strict_openclaw": "bool optional"}},
    {"method": "GET", "path": "/v1/employees", "summary": "List employees"},
    {"method": "POST", "path": "/v1/employees", "summary": "Create employee", "body": {"id": "employee id", "name": "display name", "role": "role", "runtime": "runtime id", "workspace": "path"}},
    {"method": "POST", "path": "/v1/employees/onboard", "summary": "Onboard employee with capabilities, permissions, communication, optional scaffold, and optional test task", "body": {"id": "employee id", "name": "display name", "role": "role", "runtime": "runtime id", "workspace": "path", "alias": "alias optional", "skills": "comma-separated optional", "tools": "comma-separated optional", "task_types": "comma-separated optional", "can_talk_to": "comma-separated optional", "can_assign_to": "comma-separated optional", "open_communication": "bool optional", "channel": "channel optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "create_test_task": "bool optional"}},
    {"method": "GET", "path": "/v1/employees/{employee_id}", "summary": "Show employee profile, capabilities, permissions, heartbeat, and files"},
    {"method": "PATCH", "path": "/v1/employees/{employee_id}", "summary": "Update employee profile fields through companyctl", "body": {"name": "display name optional", "role": "role optional", "runtime": "runtime id optional", "workspace": "path optional", "status": "active/candidate/archived optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "dry_run": "bool optional"}},
    {"method": "DELETE", "path": "/v1/employees/{employee_id}", "summary": "Offboard employee with dry-run, soft archive, or guarded hard delete", "body": {"hard_delete": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/profile", "summary": "Update employee profile fields through companyctl", "body": {"name": "display name optional", "role": "role optional", "runtime": "runtime id optional", "workspace": "path optional", "status": "active/candidate/archived optional", "default_user_reply_channel": "string optional", "default_user_reply_account": "string optional", "default_user_reply_to": "string optional", "default_user_reply_deliver": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/offboard", "summary": "Offboard employee with dry-run, soft archive, or guarded hard delete", "body": {"hard_delete": "bool optional", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/communication", "summary": "Enable or pause employee communication policy", "body": {"enabled": "bool required", "dry_run": "bool optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/capabilities", "summary": "Update employee capabilities", "body": {"set_skills": "comma-separated skills optional", "add_skill": "string/list optional", "set_tools": "comma-separated tools optional", "add_tool": "string/list optional", "set_task_types": "comma-separated task types optional"}},
    {"method": "POST", "path": "/v1/employees/{employee_id}/permissions", "summary": "Update employee permissions", "body": {"can_submit_tasks": "true/false/keep optional", "can_claim_tasks": "true/false/keep optional", "can_modify_kernel": "true/false/keep optional", "requires_approval_for": "comma-separated actions optional"}},
    {"method": "POST", "path": "/v1/employees/match", "summary": "Rank employees by capabilities for routing", "body": {"skills": "comma-separated skills optional", "tools": "comma-separated tools optional", "task_type": "string optional", "runtime": "runtime optional", "role": "role optional", "limit": "integer optional", "include_unavailable": "bool optional"}},
    {"method": "GET", "path": "/v1/settings/notification", "summary": "Read sanitized notification settings without secrets"},
    {"method": "POST", "path": "/v1/settings/notification", "summary": "Configure employee notification account without storing tokens", "body": {"telegram_account": "account id", "telegram_bot_token_env": "environment variable name containing token", "telegram_default_target": "chat/user target optional", "employee_notifications_enabled": "bool optional"}},
    {"method": "POST", "path": "/v1/notifications/send", "summary": "Send configured operator notification without exposing secrets", "body": {"message": "string required", "kind": "general/approval/error optional", "subject": "string optional", "target": "telegram target optional", "account": "account optional", "dry_run": "bool optional"}},
    {"method": "GET", "path": "/v1/progress/notifications", "summary": "Read pending or recent progress transition notifications", "query": {"pending_only": "bool optional", "limit": "integer optional"}},
    {"method": "GET", "path": "/v1/supervisor/delivery-loop", "summary": "Read latest autonomous supervisor delivery-loop result"},
    {"method": "POST", "path": "/v1/supervisor/delivery-loop", "summary": "Run autonomous supervisor delivery-loop once", "body": {"limit": "integer optional", "by": "actor optional"}},
    {"method": "POST", "path": "/v1/policy-blocks/report", "summary": "Report non-popup tool-policy blockers and notify operator", "body": {"source": "employee optional", "target": "employee optional", "tool": "tool name optional", "operation": "operation optional", "error": "error text required", "dry_run": "bool optional"}},
    {"method": "GET", "path": "/v1/runtimes", "summary": "List runtimes"},
    {"method": "POST", "path": "/v1/runtimes", "summary": "Register runtime", "body": {"runtime": "runtime id", "command": "command optional", "status": "registered/disabled optional", "notes": "string optional"}},
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
    {"method": "POST", "path": "/v1/tasks/{task_id}/correct", "summary": "Send supervisor correction or correction ack", "body": {"attempt_id": "attempt id", "by": "employee id", "message": "string", "ack": "bool optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/cancel", "summary": "Cancel managed long-running attempt", "body": {"attempt_id": "attempt id", "by": "employee id", "reason": "string"}},
    {"method": "GET", "path": "/v1/tasks/{task_id}/attempts", "summary": "List managed execution attempts"},
    {"method": "GET", "path": "/v1/tasks/{task_id}/conversations", "summary": "List task-bound conversations"},
    {"method": "POST", "path": "/v1/tasks/{task_id}/conversations", "summary": "Start task-bound conversation", "body": {"from": "employee id optional", "participants": "comma-separated extra participants optional", "title": "string optional", "body": "string optional", "conversation_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/messages", "summary": "List messages", "query": {"agent": "employee id required"}},
    {"method": "GET", "path": "/v1/messages/recent-direct", "summary": "Dashboard-ready recent direct messages feed", "query": {"limit": "integer optional"}},
    {"method": "GET", "path": "/v1/dashboard/communication-observability", "summary": "Dashboard-ready summary for direct messages, external mirror status, adapter-run progress, 5-layer progress heartbeat, and internal no-receipt watchdog"},
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
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/join", "summary": "Join an existing conversation as Human Owner or another employee", "body": {"agent": "employee id optional, defaults owner"}},
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/reply", "summary": "Reply to conversation", "body": {"from": "employee id", "body": "string", "message_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/approvals", "summary": "List approvals", "query": {"status": "pending/approved/denied/all optional", "agent": "employee id optional", "action": "approval action optional", "limit": "integer optional"}},
    {"method": "POST", "path": "/v1/approvals", "summary": "Request approval", "body": {"from": "employee id", "action": "string", "reason": "string", "target": "employee id optional", "risk": "P0/P1/P2/P3 optional", "approval_id": "string optional", "task_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/approvals/{approval_id}", "summary": "Show approval"},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/approve", "summary": "Approve request", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/deny", "summary": "Deny request", "body": {"by": "employee id", "reason": "string"}},
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


def route_get(path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
    if path in {"/v1", "/v1/"}:
        return HTTPStatus.OK, service_descriptor()
    if path == "/v1/openapi.json":
        return HTTPStatus.OK, openapi_descriptor()
    if path in {"/health", "/v1/health"}:
        code, payload = run_companyctl(["doctor", "--summary"])
        return HTTPStatus.OK, {"exit_code": code, **payload}
    if path in {"/v1/doctor", "/doctor"}:
        argv = ["doctor", "--summary"]
        if query_value(query, "strict_launchd") in {"1", "true", "yes"}:
            argv.append("--strict-launchd")
        if query_value(query, "strict_openclaw") in {"1", "true", "yes"}:
            argv.append("--strict-openclaw")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/employees":
        code, payload = run_companyctl(["employee", "list"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path == "/v1/employees/match":
        return HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "use POST", "path": path}
    if path == "/v1/settings/notification":
        return HTTPStatus.OK, companyctl.notification_settings()
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
                "SELECT id, event_type, source_agent, task_id, created_at, processed_at, trace_id FROM company_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "events": [dict(row) for row in rows]}
    if path == "/v1/heartbeats":
        conn = companyctl.connect()
        try:
            rows = conn.execute(
                "SELECT agent_id, runtime, workspace, status, last_seen_at FROM heartbeats ORDER BY agent_id"
            ).fetchall()
        finally:
            conn.close()
        return HTTPStatus.OK, {"ok": True, "heartbeats": [dict(row) for row in rows]}
    if path == "/v1/tasks":
        argv = ["task", "list"]
        agent = query_value(query, "agent")
        status = query_value(query, "status")
        if agent:
            argv.extend(["--agent", agent])
        if status:
            argv.extend(["--status", status])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/conversations"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/conversations").strip("/")
        code, payload = run_companyctl(["task", "conversations", "--task-id", task_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/attempts"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/attempts").strip("/")
        code, payload = run_companyctl(["task", "attempts", "--task-id", task_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/"):
        task_id = path.removeprefix("/v1/tasks/").strip("/")
        code, payload = run_companyctl(["task", "show", "--task-id", task_id])
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
    if path == "/v1/dashboard/communication-observability":
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
            return HTTPStatus.OK, {"ok": True, **company_dashboard.communication_observability_summary(summary)}
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
        code, payload = run_companyctl(["approval", "show", "--approval-id", approval_id])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
    if path == "/v1/settings/notification":
        payload = companyctl.update_notification_settings(body)
        return (HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_REQUEST), payload
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
        argv = ["task", "correct", "--task-id", task_id, "--attempt-id", str(body.get("attempt_id", "")), "--by", str(body.get("by", "")), "--message", str(body.get("message", ""))]
        if truthy(body.get("ack")):
            argv.append("--ack")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
    if path.startswith("/v1/tasks/") and path.endswith("/cancel"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/cancel").strip("/")
        code, payload = run_companyctl(["task", "cancel", "--task-id", task_id, "--attempt-id", str(body.get("attempt_id", "")), "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/retry"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/retry").strip("/")
        code, payload = run_companyctl(["task", "retry", "--task-id", task_id, "--by", str(body.get("by", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path.startswith("/v1/tasks/") and path.endswith("/reassign"):
        task_id = path.removeprefix("/v1/tasks/").removesuffix("/reassign").strip("/")
        code, payload = run_companyctl(["task", "reassign", "--task-id", task_id, "--by", str(body.get("by", "")), "--to", str(body.get("to", "")), "--reason", str(body.get("reason", ""))])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
                str(body.get("agent", "owner") or "owner"),
                "--conversation-id",
                conversation_id,
            ]
        )
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in CONSOLE_PATHS:
            if CONSOLE_TEMPLATE.exists():
                self.send_html(HTTPStatus.OK, CONSOLE_TEMPLATE.read_text(encoding="utf-8"))
            else:
                self.send_html(HTTPStatus.NOT_FOUND, "<h1>console template missing</h1><p>expected at dashboard_templates/console.html</p>")
            return
        query = parse_qs(parsed.query)
        status, payload = route_get(parsed.path, query)
        self.send_json(status, payload)

    def do_POST(self) -> None:
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_post(parsed.path, body)
        except SystemExit as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        self.send_json(status, payload)

    def do_PATCH(self) -> None:
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_patch(parsed.path, body)
        except SystemExit as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
        self.send_json(status, payload)

    def do_DELETE(self) -> None:
        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        parsed = urlparse(self.path)
        try:
            status, payload = route_delete(parsed.path, body)
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
