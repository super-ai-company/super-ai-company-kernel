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
    if path == "/v1/adapter-runs":
        argv = ["runtime", "adapter-runs"]
        agent = query_value(query, "agent")
        status = query_value(query, "status")
        if agent:
            argv.extend(["--agent", agent])
        if status:
            argv.extend(["--status", status])
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
    if path == "/v1/heartbeats":
        code, payload = run_companyctl(["heartbeat", "--agent", str(body.get("agent", ""))])
        return (HTTPStatus.CREATED if code == 0 else HTTPStatus.BAD_REQUEST), {"exit_code": code, **payload}
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
