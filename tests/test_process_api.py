"""工艺版本 HTTP JSON 接口、稳定错误码与幂等头的测试。"""

from __future__ import annotations

import json
import unittest

from photon_fab.api import JsonApplication
from photon_fab.service import PhotonService

P32 = {
    "deposition_temp_c": 250.0,
    "chamber_pressure_pa": 5.0,
    "gas_flow_sccm": 120.0,
    "rf_power_w": 300.0,
    "etch_time_s": 45.0,
    "bake_temp_c": 120.0,
    "exposure_dose_mj_cm2": 220.0,
}
P33 = {**P32, "deposition_temp_c": 265.0}


def _payload(body: dict) -> bytes:
    return json.dumps(body).encode()


class ProcessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.app = JsonApplication(self.service)
        login = self.app.handle("POST", "/login", {}, _payload({"user_id": "admin", "password": "photon-admin"}))
        self.headers = {"authorization": "Bearer " + login.body["token"]}

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_register_freeze_derive_and_get(self) -> None:
        created = self.app.handle(
            "POST", "/processes", self.headers | {"idempotency-key": "k1"},
            _payload({"process_id": "P3", "product": "CMOS image sensor", "parameters": P32, "change_reason": "P3.2"}),
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["version"], 1)

        frozen = self.app.handle("POST", "/processes/P3/1/freeze", self.headers)
        self.assertEqual(frozen.status, 200)
        self.assertEqual(frozen.body["state"], "frozen")

        derived = self.app.handle(
            "POST", "/processes/P3/derive", self.headers | {"idempotency-key": "k2"},
            _payload({"parent_version": 1, "parameters": P33, "change_reason": "P3.3"}),
        )
        self.assertEqual(derived.status, 201)
        self.assertEqual(derived.body["parent_version"], 1)
        self.assertEqual(derived.body["version"], 2)

        listing = self.app.handle("GET", "/processes/P3", self.headers)
        self.assertEqual([v["version"] for v in listing.body["versions"]], [1, 2])

        one = self.app.handle("GET", "/processes/P3/2", self.headers)
        self.assertEqual(one.status, 200)
        self.assertEqual(one.body["parameters"]["deposition_temp_c"], 265.0)

    def test_idempotent_register_header_returns_same_record(self) -> None:
        body = _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "r"})
        first = self.app.handle("POST", "/processes", self.headers | {"idempotency-key": "dup"}, body)
        second = self.app.handle("POST", "/processes", self.headers | {"idempotency-key": "dup"}, body)
        self.assertEqual(first.status, 201)
        self.assertEqual(first.body, second.body)

    def test_stable_invalid_state_error_on_double_freeze(self) -> None:
        self.app.handle(
            "POST", "/processes", self.headers,
            _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "r"}),
        )
        self.app.handle("POST", "/processes/P3/1/freeze", self.headers)
        again = self.app.handle("POST", "/processes/P3/1/freeze", self.headers)
        self.assertEqual(again.status, 409)
        self.assertEqual(again.body["error"]["code"], "invalid_state")

    def test_frozen_update_rejected(self) -> None:
        self.app.handle(
            "POST", "/processes", self.headers,
            _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "r"}),
        )
        self.app.handle("POST", "/processes/P3/1/freeze", self.headers)
        response = self.app.handle(
            "PUT", "/processes/P3/1", self.headers,
            _payload({"parameters": P33, "change_reason": "想改发布版"}),
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "invalid_state")

    def test_validation_error_is_422(self) -> None:
        response = self.app.handle(
            "POST", "/processes", self.headers,
            _payload({"process_id": "P3", "product": "sensor", "parameters": {**P32, "rf_power_w": 99999},
                      "change_reason": "r"}),
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_not_found_is_404(self) -> None:
        response = self.app.handle("GET", "/processes/UNKNOWN/1", self.headers)
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")

    def test_bind_and_trace_route(self) -> None:
        self.app.handle(
            "POST", "/processes", self.headers,
            _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "P3.2"}),
        )
        self.app.handle("POST", "/processes/P3/1/freeze", self.headers)
        self.app.handle(
            "POST", "/lots", self.headers,
            _payload({"lot_id": "LOT-A", "product": "sensor", "process_rev": "P3-v1", "wafer_count": 10}),
        )
        bound = self.app.handle(
            "POST", "/lots/LOT-A/process-binding", self.headers | {"idempotency-key": "b1"},
            _payload({"process_id": "P3", "version": 1, "change_reason": "试产"}),
        )
        self.assertEqual(bound.status, 201)

        current = self.app.handle("GET", "/lots/LOT-A/process", self.headers)
        self.assertEqual(current.body["version"], 1)

        trace = self.app.handle("GET", "/lots/LOT-A/trace", self.headers)
        self.assertEqual(trace.status, 200)
        self.assertEqual(trace.body["binding"]["process_id"], "P3")
        self.assertEqual(len(trace.body["lineage"]), 1)

    def test_bind_draft_version_is_invalid_state(self) -> None:
        self.app.handle(
            "POST", "/processes", self.headers,
            _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "r"}),
        )
        self.app.handle(
            "POST", "/lots", self.headers,
            _payload({"lot_id": "LOT-A", "product": "sensor", "process_rev": "x", "wafer_count": 1}),
        )
        response = self.app.handle(
            "POST", "/lots/LOT-A/process-binding", self.headers,
            _payload({"process_id": "P3", "version": 1}),
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "invalid_state")

    def test_unknown_route_and_bad_json(self) -> None:
        missing = self.app.handle("GET", "/nope", self.headers)
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.body["error"]["code"], "route_not_found")
        bad = self.app.handle("POST", "/processes", self.headers, b"not-json")
        self.assertEqual(bad.status, 422)
        self.assertEqual(bad.body["error"]["code"], "validation_failed")

    def test_missing_token_is_forbidden(self) -> None:
        response = self.app.handle(
            "POST", "/processes", {},
            _payload({"process_id": "P3", "product": "sensor", "parameters": P32, "change_reason": "r"}),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
