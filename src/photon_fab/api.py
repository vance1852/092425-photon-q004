"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import PhotonError, ValidationFailed
from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    # 单一 SQLite 连接被多个请求线程共享，串行化派发以保证事务完整。
    _lock = threading.RLock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _idempotency_key(self, body: dict) -> str:
        return self.headers.get("Idempotency-Key", "").strip() or str(body.get("idempotency_key", "")).strip()

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def _dispatch(self, method: str) -> None:
        with Handler._lock:
            self._route(method)

    def _route(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            query = parse_qs(parsed.query)
            if method == "GET" and parsed.path == "/health":
                return self._json(200, {"status": "ok", "service": "photon-fab"})
            body = self._body() if method == "POST" else {}
            token = self._token()
            if method == "POST" and parts == ["login"]:
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            if method == "POST" and parts == ["lots"]:
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"],
                    process_version_id=body.get("process_version_id"),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token, parts[1], body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"],
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "process-version":
                return self._json(200, self.service.bind_lot_version(
                    token, parts[1], body["version_id"], self._idempotency_key(body),
                ))
            if method == "POST" and parts == ["process-versions"]:
                return self._json(201, self.service.register_process_version(
                    token, body["version_id"], body["product"], body["version_label"], body.get("params"),
                    self._idempotency_key(body),
                    parent_version_id=body.get("parent_version_id"),
                    change_reason=body.get("change_reason", ""),
                ))
            if method == "GET" and parts == ["process-versions"]:
                product = query.get("product", [None])[0]
                return self._json(200, {"versions": self.service.list_process_versions(token, product)})
            if method == "GET" and len(parts) == 2 and parts[0] == "process-versions":
                return self._json(200, self.service.get_process_version(token, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "process-versions" and parts[2] == "chain":
                return self._json(200, {"chain": self.service.process_version_chain(token, parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "process-versions" and parts[2] == "events":
                return self._json(200, {"events": self.service.version_events(token, parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "process-versions" and parts[2] == "params":
                return self._json(200, self.service.update_process_params(
                    token, parts[1], body.get("params"), self._idempotency_key(body),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "process-versions" and parts[2] == "freeze":
                return self._json(200, self.service.freeze_process_version(
                    token, parts[1], self._idempotency_key(body),
                ))
            return self._json(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except PhotonError as exc:
            return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except PermissionError as exc:
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        except (KeyError, ValueError) as exc:
            return self._json(422, {"error": {"code": "validation_failed", "message": str(exc)}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format, *args):
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
