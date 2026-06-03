from __future__ import annotations

import argparse
import importlib.util
import json
from concurrent import futures
from dataclasses import dataclass

from . import api_gateway


@dataclass
class DescribeRequest:
    pass


@dataclass
class RouteRequest:
    path: str = ""
    query: dict[str, str] | None = None
    body_json: str = "{}"


@dataclass
class ApiResponse:
    status: int
    body_json: str


def response(status: int, body: dict) -> ApiResponse:
    return ApiResponse(status=int(status), body_json=json.dumps(body, ensure_ascii=False))


def query_for_route(request: RouteRequest) -> dict[str, list[str]]:
    return {str(key): [str(value)] for key, value in (request.query or {}).items()}


class CompanyKernelService:
    def Describe(self, request: DescribeRequest, context: object | None = None) -> ApiResponse:
        body = api_gateway.service_descriptor()
        return response(200, body)

    def Get(self, request: RouteRequest, context: object | None = None) -> ApiResponse:
        if not request.path:
            return response(400, {"ok": False, "error": "path is required"})
        status, body = api_gateway.route_get(request.path, query_for_route(request))
        return response(status, body)

    def Post(self, request: RouteRequest, context: object | None = None) -> ApiResponse:
        if not request.path:
            return response(400, {"ok": False, "error": "path is required"})
        try:
            body = json.loads(request.body_json or "{}")
        except json.JSONDecodeError as exc:
            return response(400, {"ok": False, "error": f"invalid body_json: {exc}"})
        if not isinstance(body, dict):
            return response(400, {"ok": False, "error": "body_json must decode to object"})
        status, result = api_gateway.route_post(request.path, body)
        return response(status, result)


def grpc_available() -> bool:
    return bool(importlib.util.find_spec("grpc"))


def run_server(host: str, port: int, max_workers: int = 8) -> None:
    if not grpc_available():
        raise SystemExit("grpcio is not installed; install grpcio and generated stubs to run the gRPC server")
    import grpc  # type: ignore

    try:
        from . import company_kernel_pb2_grpc  # type: ignore
    except ImportError as exc:
        raise SystemExit("generated gRPC stubs are missing; run python -m grpc_tools.protoc for docs/company_kernel.proto") from exc

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    company_kernel_pb2_grpc.add_CompanyKernelServicer_to_server(CompanyKernelService(), server)
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel gRPC Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--check", action="store_true", help="check whether grpcio is installed and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        print(json.dumps({"ok": grpc_available(), "grpcio": grpc_available()}, ensure_ascii=False))
        return 0 if grpc_available() else 1
    run_server(args.host, args.port, max_workers=args.max_workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
