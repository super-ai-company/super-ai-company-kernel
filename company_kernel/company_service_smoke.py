from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.request

from . import api_gateway
from . import api_grpc
from . import api_rpc


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def start_thread(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    time.sleep(0.2)
    return thread


def run_smoke() -> dict:
    rest_port = free_port()
    rpc_port = free_port()
    start_thread(api_gateway.run_server, "127.0.0.1", rest_port, True)
    start_thread(api_rpc.run_server, "127.0.0.1", rpc_port, True)

    rest = get_json(f"http://127.0.0.1:{rest_port}/v1/health")
    rpc_describe = get_json(f"http://127.0.0.1:{rpc_port}/rpc")
    rpc_health = post_json(
        f"http://127.0.0.1:{rpc_port}/rpc",
        {"jsonrpc": "2.0", "id": "health", "method": "company.get", "params": {"path": "/v1/health", "query": {}}},
    )
    grpc_ready = api_grpc.grpc_available()
    return {
        "ok": bool(rest.get("ok")) and bool(rpc_describe.get("ok")) and rpc_health.get("result", {}).get("status") == 200,
        "rest": {"port": rest_port, "ok": bool(rest.get("ok")), "issues": rest.get("issues", [])},
        "rpc": {"port": rpc_port, "describe_ok": bool(rpc_describe.get("ok")), "health_status": rpc_health.get("result", {}).get("status")},
        "grpc": {"available": grpc_ready, "check": "ready" if grpc_ready else "grpcio_not_installed"},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel API service smoke test")
    parser.add_argument("--json-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_smoke()
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json_only else 2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
