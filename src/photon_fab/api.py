"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import PhotonService


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    """将 HTTP 路由映射到 PhotonService，便于无网络单元测试。"""

    def __init__(self, service: PhotonService) -> None:
        self.service = service

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise _validation("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _token(headers: Mapping[str, str]) -> str:
        return headers.get("authorization", "").removeprefix("Bearer ").strip()

    @staticmethod
    def _idem(headers: Mapping[str, str], payload: Mapping[str, Any]) -> str | None:
        key = headers.get("idempotency-key", "").strip()
        return key or str(payload.get("idempotency_key", "")).strip() or None

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        service = self.service
        try:
            payload = self._json_body(body) if method in {"POST", "PUT", "PATCH"} else {}
            token = self._token(normalized)

            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "photon-fab"})

            if method == "POST" and path == "/login":
                return Response(200, {"token": service.auth.login(payload["user_id"], payload["password"])})

            # 工艺版本管理
            if method == "POST" and path == "/processes":
                result = service.register_process(
                    token, payload["process_id"], payload["product"], payload["parameters"],
                    str(payload.get("change_reason", "")), self._idem(normalized, payload),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "processes":
                return Response(200, {"versions": service.list_process_versions(token, parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "processes":
                return Response(200, service.get_process_version(token, parts[1], int(parts[2])))
            if method == "PUT" and len(parts) == 3 and parts[0] == "processes":
                result = service.update_process(
                    token, parts[1], int(parts[2]), payload["parameters"],
                    str(payload.get("change_reason", "")),
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 4 and parts[0] == "processes" and parts[3] == "freeze":
                return Response(200, service.freeze_process(token, parts[1], int(parts[2])))
            if method == "POST" and len(parts) == 3 and parts[0] == "processes" and parts[2] == "derive":
                result = service.derive_process(
                    token, parts[1], int(payload["parent_version"]), payload["parameters"],
                    str(payload.get("change_reason", "")), payload.get("new_process_id"),
                    self._idem(normalized, payload),
                )
                return Response(201, result)

            # 批次与工艺绑定 / 追溯
            if method == "POST" and path == "/lots":
                result = service.create_lot(
                    token, payload["lot_id"], payload["product"], payload["process_rev"],
                    int(payload["wafer_count"]),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "lots":
                return Response(200, service.get_lot(token, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                result = service.add_measurement(
                    token, parts[1], float(payload["wavelength_nm"]), float(payload["response"]),
                    float(payload.get("noise", 0.0)), payload["instrument"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return Response(200, service.analyze(token, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "approval":
                return Response(200, service.approve(token, parts[1], payload["decision"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "process-binding":
                result = service.bind_lot_process(
                    token, parts[1], payload["process_id"], int(payload["version"]),
                    str(payload.get("change_reason", "")), self._idem(normalized, payload),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "process":
                return Response(200, service.get_lot_process(token, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "trace":
                return Response(200, service.lot_trace(token, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return Response(200, {"events": service.audit(token, parts[1])})

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except PermissionError as exc:
            return Response(403, {"error": {"code": "forbidden", "message": str(exc)}})
        except KeyError as exc:
            return Response(404, {"error": {"code": "not_found", "message": str(exc).strip("'")}})
        except (TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def _validation(message: str):
    from .errors import ValidationFailed

    return ValidationFailed(message)


class Handler(BaseHTTPRequestHandler):
    application: JsonApplication | None = None
    service = PhotonService()

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        app = self.application or JsonApplication(self.service)
        response = app.handle(self.command, self.path, dict(self.headers.items()), body)
        encoded = json.dumps(response.body, ensure_ascii=False).encode()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = PhotonService(args.database)
    service.bootstrap_admin()
    Handler.service = service
    Handler.application = JsonApplication(service)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
