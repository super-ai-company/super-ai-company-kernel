from __future__ import annotations

import argparse
import contextlib
import io
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import companyctl


API_VERSION = "v1"
API_CAPABILITIES = [
    "health",
    "doctor",
    "tasks",
    "messages",
    "conversations",
    "approvals",
    "heartbeats",
    "adapter_runs",
    "projects",
]
API_ENDPOINTS = [
    {"method": "GET", "path": "/v1/health", "summary": "Company Kernel health summary"},
    {"method": "GET", "path": "/v1/doctor", "summary": "Doctor summary", "query": {"strict_launchd": "bool optional"}},
    {"method": "GET", "path": "/v1/tasks", "summary": "List tasks", "query": {"agent": "employee id optional", "status": "task status optional"}},
    {"method": "POST", "path": "/v1/tasks", "summary": "Submit task", "body": {"from": "employee id", "to": "employee id", "title": "string", "description": "string optional", "task_id": "string optional", "priority": "P0/P1/P2/P3 optional", "requires_approval": "action optional", "approval_id": "string optional"}},
    {"method": "GET", "path": "/v1/tasks/{task_id}", "summary": "Show task"},
    {"method": "POST", "path": "/v1/tasks/{task_id}/claim", "summary": "Claim task", "body": {"agent": "employee id", "lease_seconds": "integer optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/done", "summary": "Complete task", "body": {"agent": "employee id", "summary": "string", "evidence": "path"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/block", "summary": "Block task", "body": {"agent": "employee id", "blocker": "string"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/reopen", "summary": "Reopen blocked/interrupted task", "body": {"by": "employee id", "reason": "string", "status": "submitted/claimed optional"}},
    {"method": "POST", "path": "/v1/tasks/{task_id}/reassign", "summary": "Reassign task to another employee", "body": {"by": "employee id", "to": "employee id", "reason": "string"}},
    {"method": "GET", "path": "/v1/messages", "summary": "List messages", "query": {"agent": "employee id required"}},
    {"method": "POST", "path": "/v1/messages", "summary": "Send message", "body": {"from": "employee id", "to": "employee id", "body": "string", "message_id": "string optional"}},
    {"method": "GET", "path": "/v1/conversations", "summary": "List conversations for an agent", "query": {"agent": "employee id required"}},
    {"method": "POST", "path": "/v1/conversations", "summary": "Start conversation", "body": {"from": "employee id", "participants": "comma-separated employee ids", "title": "string", "body": "string", "conversation_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/conversations/{conversation_id}", "summary": "Show conversation"},
    {"method": "POST", "path": "/v1/conversations/{conversation_id}/reply", "summary": "Reply to conversation", "body": {"from": "employee id", "body": "string", "message_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/approvals", "summary": "List approvals", "query": {"status": "pending/approved/denied/all optional", "agent": "employee id optional", "action": "approval action optional", "limit": "integer optional"}},
    {"method": "POST", "path": "/v1/approvals", "summary": "Request approval", "body": {"from": "employee id", "action": "string", "reason": "string", "target": "employee id optional", "risk": "P0/P1/P2/P3 optional", "approval_id": "string optional", "task_id": "string optional", "evidence": "path optional"}},
    {"method": "GET", "path": "/v1/approvals/{approval_id}", "summary": "Show approval"},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/approve", "summary": "Approve request", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/approvals/{approval_id}/deny", "summary": "Deny request", "body": {"by": "employee id", "reason": "string"}},
    {"method": "POST", "path": "/v1/heartbeats", "summary": "Write employee heartbeat", "body": {"agent": "employee id"}},
    {"method": "GET", "path": "/v1/projects", "summary": "List projects", "query": {"status": "active/paused/completed/blocked/all optional"}},
    {"method": "POST", "path": "/v1/projects", "summary": "Create project", "body": {"project_id": "string optional", "title": "string", "goal": "string optional", "owner": "employee id", "status": "active/paused/completed/blocked optional", "acceptance": "semicolon-separated criteria optional"}},
    {"method": "GET", "path": "/v1/projects/{project_id}", "summary": "Show project with linked tasks and plan items"},
    {"method": "POST", "path": "/v1/projects/{project_id}/tasks", "summary": "Link task to project", "body": {"task_id": "string"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/plan-items", "summary": "Add project plan item", "body": {"title": "string", "status": "planned/in_progress/done/completed/blocked/cancelled optional", "owner": "employee id optional", "due_at": "string optional", "task_id": "string optional", "plan_id": "string optional"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/plan-items/{plan_id}/status", "summary": "Update project plan item status", "body": {"status": "planned/in_progress/done/completed/blocked/cancelled"}},
    {"method": "POST", "path": "/v1/projects/{project_id}/status", "summary": "Update project status", "body": {"status": "active/paused/completed/blocked"}},
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
        },
        "protocols": {
            "rest": True,
            "grpc": False,
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


def run_companyctl(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = companyctl.main(argv)
    raw = buf.getvalue().strip()
    return code, json.loads(raw) if raw else {}


def query_value(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name, [])
    return values[0] if values else default


def route_get(path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
    if path in {"/v1", "/v1/"}:
        return HTTPStatus.OK, service_descriptor()
    if path == "/v1/openapi.json":
        return HTTPStatus.OK, openapi_descriptor()
    if path in {"/health", "/v1/health"}:
        code, payload = run_companyctl(["doctor", "--summary"])
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
    if path in {"/v1/doctor", "/doctor"}:
        argv = ["doctor", "--summary"]
        if query_value(query, "strict_launchd") in {"1", "true", "yes"}:
            argv.append("--strict-launchd")
        code, payload = run_companyctl(argv)
        return (HTTPStatus.OK if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
    if path == "/v1/projects":
        argv = ["project", "list"]
        status = query_value(query, "status")
        if status:
            argv.extend(["--status", status])
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


def route_post(path: str, body: dict) -> tuple[int, dict]:
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
    if path == "/v1/messages":
        argv = ["message", "send", "--from", str(body.get("from", "")), "--to", str(body.get("to", "")), "--body", str(body.get("body", ""))]
        if body.get("message_id"):
            argv.extend(["--message-id", str(body["message_id"])])
        code, payload = run_companyctl(argv)
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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

    def send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
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
        status, payload = route_post(parsed.path, body)
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
