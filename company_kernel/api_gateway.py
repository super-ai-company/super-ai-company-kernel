from __future__ import annotations

import argparse
import contextlib
import io
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import companyctl


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
