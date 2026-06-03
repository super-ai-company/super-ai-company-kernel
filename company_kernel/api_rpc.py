from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import api_gateway


RPC_VERSION = "2.0"
RPC_METHODS = {
    "company.describe": "Return REST/RPC service descriptor and governance contract.",
    "company.get": "Route a read request through the Company Kernel service layer.",
    "company.post": "Route a write request through the Company Kernel service layer.",
}


class RpcError(ValueError):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def rpc_descriptor() -> dict:
    descriptor = api_gateway.service_descriptor()
    return {
        **descriptor,
        "name": "Company Kernel RPC Gateway",
        "rpc": {
            "jsonrpc": RPC_VERSION,
            "endpoint": "/rpc",
            "methods": RPC_METHODS,
            "grpc_proto": "docs/company_kernel.proto",
        },
    }


def normalize_query(value: object) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RpcError(-32602, "params.query must be an object")
    normalized: dict[str, list[str]] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, list):
            normalized[str(key)] = [str(entry) for entry in item]
        else:
            normalized[str(key)] = [str(item)]
    return normalized


def require_params(params: object) -> dict:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise RpcError(-32602, "params must be an object")
    return params


def handle_rpc(payload: dict) -> dict:
    if payload.get("jsonrpc", RPC_VERSION) != RPC_VERSION:
        raise RpcError(-32600, "jsonrpc must be 2.0")
    method = str(payload.get("method", ""))
    request_id = payload.get("id")
    params = require_params(payload.get("params", {}))

    if method == "company.describe":
        return {"jsonrpc": RPC_VERSION, "id": request_id, "result": rpc_descriptor()}
    if method == "company.get":
        path = str(params.get("path", ""))
        if not path:
            raise RpcError(-32602, "params.path is required")
        status, result = api_gateway.route_get(path, normalize_query(params.get("query", {})))
        return {"jsonrpc": RPC_VERSION, "id": request_id, "result": {"status": int(status), "body": result}}
    if method == "company.post":
        path = str(params.get("path", ""))
        if not path:
            raise RpcError(-32602, "params.path is required")
        body = params.get("body", {})
        if not isinstance(body, dict):
            raise RpcError(-32602, "params.body must be an object")
        status, result = api_gateway.route_post(path, body)
        return {"jsonrpc": RPC_VERSION, "id": request_id, "result": {"status": int(status), "body": result}}
    raise RpcError(-32601, f"method not found: {method}")


def error_response(request_id: object, exc: RpcError) -> dict:
    error = {"code": exc.code, "message": exc.message}
    if exc.data:
        error["data"] = exc.data
    return {"jsonrpc": RPC_VERSION, "id": request_id, "error": error}


class RpcHandler(BaseHTTPRequestHandler):
    server_version = "CompanyKernelRPC/0.1"

    def send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if urlparse(self.path).path in {"/", "/rpc"}:
            self.send_json(HTTPStatus.OK, rpc_descriptor())
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found", "path": self.path})

    def do_POST(self) -> None:
        request_id = None
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise RpcError(-32600, "request body must be an object")
            request_id = payload.get("id")
            self.send_json(HTTPStatus.OK, handle_rpc(payload))
        except json.JSONDecodeError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, error_response(request_id, RpcError(-32700, f"parse error: {exc}")))
        except RpcError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST if exc.code in {-32600, -32602, -32700} else HTTPStatus.NOT_FOUND, error_response(request_id, exc))

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)


def run_server(host: str, port: int, quiet: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), RpcHandler)
    server.quiet = quiet
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel RPC Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_server(args.host, args.port, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
