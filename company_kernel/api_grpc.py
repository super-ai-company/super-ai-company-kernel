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


def encode_response(result: ApiResponse) -> bytes:
    return json.dumps({"status": result.status, "body_json": result.body_json}, ensure_ascii=False).encode("utf-8")


def decode_route_request(raw: bytes) -> RouteRequest:
    if not raw:
        return RouteRequest()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request payload must decode to object")
    query = payload.get("query") or {}
    if not isinstance(query, dict):
        raise ValueError("query must be an object")
    return RouteRequest(path=str(payload.get("path", "")), query={str(k): str(v) for k, v in query.items()}, body_json=str(payload.get("body_json", "{}")))


def decode_describe_request(raw: bytes) -> DescribeRequest:
    if raw:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("describe payload must decode to object")
    return DescribeRequest()


def generic_method_handlers(service: CompanyKernelService) -> dict:
    def describe(raw: bytes) -> bytes:
        return encode_response(service.Describe(decode_describe_request(raw)))

    def get(raw: bytes) -> bytes:
        return encode_response(service.Get(decode_route_request(raw)))

    def post(raw: bytes) -> bytes:
        return encode_response(service.Post(decode_route_request(raw)))

    return {"Describe": describe, "Get": get, "Post": post}


def add_generic_service(server: object, grpc_module: object, service: CompanyKernelService) -> None:
    handlers = generic_method_handlers(service)
    method_handlers = {
        name: grpc_module.unary_unary_rpc_method_handler(handler, request_deserializer=lambda raw: raw, response_serializer=lambda raw: raw)
        for name, handler in handlers.items()
    }
    generic_handler = grpc_module.method_handlers_generic_handler("company.kernel.v1.CompanyKernel", method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


def run_server(host: str, port: int, max_workers: int = 8) -> None:
    if not grpc_available():
        raise SystemExit("grpcio is not installed; install grpcio to run the gRPC server")
    import grpc  # type: ignore

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    add_generic_service(server, grpc, CompanyKernelService())
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
