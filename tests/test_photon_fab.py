from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from photon_fab.api import Handler
from photon_fab.errors import Conflict, InvalidState, NotFound, ValidationFailed
from photon_fab.service import PhotonService

PARAMS_P32 = {
    "temperature_c": 850.0,
    "pressure_pa": 133.3,
    "duration_min": 120.0,
    "gas_flow_sccm": 500.0,
    "target_wavelength_nm": 1310.0,
}
PARAMS_P33 = dict(PARAMS_P32, temperature_c=870.0, duration_min=135.0, target_wavelength_nm=1550.0)


class ProcessVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        for user_id, role in (("eng", "engineer"), ("qa", "quality"), ("op", "operator")):
            self.service.auth.create_user(user_id, f"{user_id}-password", role)
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.engineer = self.service.auth.login("eng", "eng-password")
        self.quality = self.service.auth.login("qa", "qa-password")
        self.operator = self.service.auth.login("op", "op-password")

    def _register_p32(self, key: str = "k-p32") -> dict:
        return self.service.register_process_version(self.engineer, "PV-3.2", "CIS", "P3.2", PARAMS_P32, key)

    def _freeze_p32(self, key: str = "k-p32-freeze") -> dict:
        return self.service.freeze_process_version(self.quality, "PV-3.2", key)

    def test_register_freeze_derive_bind_chain(self) -> None:
        root = self._register_p32()
        self.assertEqual(root["state"], "draft")
        self.assertEqual(root["params"]["temperature_c"], 850.0)
        self.assertIsNone(root["parent_version_id"])
        frozen = self._freeze_p32()
        self.assertEqual(frozen["state"], "frozen")
        self.assertIsNotNone(frozen["frozen_at"])
        child = self.service.register_process_version(
            self.engineer, "PV-3.3", "CIS", "P3.3", PARAMS_P33, "k-p33",
            parent_version_id="PV-3.2", change_reason="目标波长调整至 1550nm",
        )
        self.assertEqual(child["parent_version_id"], "PV-3.2")
        self.service.freeze_process_version(self.quality, "PV-3.3", "k-p33-freeze")
        lot = self.service.create_lot(self.engineer, "LOT-1", "CIS", "P3.3", 8, process_version_id="PV-3.3")
        self.assertEqual(lot["process_version_id"], "PV-3.3")
        chain = lot["process_chain"]
        self.assertEqual([v["version_label"] for v in chain], ["P3.3", "P3.2"])
        self.assertEqual(chain[0]["params"]["target_wavelength_nm"], 1550.0)
        self.assertEqual(chain[1]["change_reason"], "")
        self.assertEqual(chain[0]["change_reason"], "目标波长调整至 1550nm")
        # 新版本发布后，父版本历史快照保持可读且不变
        snapshot = self.service.get_process_version(self.operator, "PV-3.2")
        self.assertEqual(snapshot["params"], PARAMS_P32)
        self.assertEqual(snapshot["bound_lots"], 0)

    def test_param_validation_rejects_bad_input(self) -> None:
        for bad in (
            None,
            {"temperature_c": 1},
            dict(PARAMS_P32, unknown_key=1),
            dict(PARAMS_P32, temperature_c=-1),
            dict(PARAMS_P32, pressure_pa=0),
            dict(PARAMS_P32, duration_min=0),
            dict(PARAMS_P32, target_wavelength_nm=float("nan")),
            dict(PARAMS_P32, gas_flow_sccm=True),
        ):
            with self.assertRaises(ValidationFailed, msg=repr(bad)):
                self.service.register_process_version(self.engineer, "PV-BAD", "CIS", "P9.9", bad, "k-bad")

    def test_update_params_only_before_freeze(self) -> None:
        self._register_p32()
        updated = self.service.update_process_params(
            self.engineer, "PV-3.2", dict(PARAMS_P32, temperature_c=860.0), "k-upd-1"
        )
        self.assertEqual(updated["params"]["temperature_c"], 860.0)
        self._freeze_p32()
        with self.assertRaises(InvalidState) as ctx:
            self.service.update_process_params(self.engineer, "PV-3.2", PARAMS_P32, "k-upd-2")
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_bound_version_cannot_be_modified(self) -> None:
        self._register_p32()
        self._freeze_p32()
        self.service.create_lot(self.engineer, "LOT-1", "CIS", "P3.2", 8, process_version_id="PV-3.2")
        with self.assertRaises(InvalidState) as ctx:
            self.service.update_process_params(self.engineer, "PV-3.2", PARAMS_P32, "k-upd-bound")
        self.assertIn("绑定批次", str(ctx.exception))

    def test_bind_requires_frozen_version(self) -> None:
        self._register_p32()
        self.service.create_lot(self.engineer, "LOT-1", "CIS", "P3.2", 8)
        with self.assertRaises(InvalidState):
            self.service.bind_lot_version(self.engineer, "LOT-1", "PV-3.2", "k-bind-draft")
        with self.assertRaises(InvalidState):
            self.service.create_lot(self.engineer, "LOT-2", "CIS", "P3.2", 8, process_version_id="PV-3.2")

    def test_rebind_is_rejected(self) -> None:
        self._register_p32()
        self._freeze_p32()
        self.service.register_process_version(
            self.engineer, "PV-3.3", "CIS", "P3.3", PARAMS_P33, "k-p33",
            parent_version_id="PV-3.2", change_reason="调整",
        )
        self.service.freeze_process_version(self.quality, "PV-3.3", "k-p33-freeze")
        self.service.create_lot(self.engineer, "LOT-1", "CIS", "P3.2", 8)
        self.service.bind_lot_version(self.engineer, "LOT-1", "PV-3.2", "k-bind-1")
        with self.assertRaises(InvalidState) as ctx:
            self.service.bind_lot_version(self.engineer, "LOT-1", "PV-3.3", "k-bind-2")
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_illegal_transitions_raise_stable_errors(self) -> None:
        self._register_p32()
        self._freeze_p32()
        with self.assertRaises(InvalidState) as ctx:
            self.service.freeze_process_version(self.quality, "PV-3.2", "k-freeze-again")
        self.assertEqual(ctx.exception.code, "invalid_state")
        with self.assertRaises(NotFound) as ctx:
            self.service.freeze_process_version(self.quality, "PV-MISSING", "k-freeze-missing")
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(Conflict) as ctx:
            self.service.create_lot(self.engineer, "LOT-DUP", "CIS", "P3.2", 8)
            self.service.create_lot(self.engineer, "LOT-DUP", "CIS", "P3.2", 8)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_idempotent_replay_and_payload_conflict(self) -> None:
        first = self._register_p32()
        second = self._register_p32()
        self.assertEqual(first, second)
        count = self.service.db.execute("SELECT count(*) FROM process_versions").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(Conflict) as ctx:
            self.service.register_process_version(self.engineer, "PV-OTHER", "CIS", "P3.2", PARAMS_P32, "k-p32")
        self.assertEqual(ctx.exception.code, "conflict")
        # 冻结重复提交：同键重放返回首次响应，换键则报稳定状态错误
        frozen = self._freeze_p32()
        replay = self._freeze_p32()
        self.assertEqual(frozen, replay)
        # 绑定重复提交：同键重放不重复产生绑定事件
        self.service.create_lot(self.engineer, "LOT-1", "CIS", "P3.2", 8)
        bound = self.service.bind_lot_version(self.engineer, "LOT-1", "PV-3.2", "k-bind-1")
        bound_replay = self.service.bind_lot_version(self.engineer, "LOT-1", "PV-3.2", "k-bind-1")
        self.assertEqual(bound, bound_replay)
        events = [e for e in self.service.audit(self.admin, "LOT-1") if e["event_type"] == "process_bound"]
        self.assertEqual(len(events), 1)

    def test_derived_version_requires_frozen_parent_and_reason(self) -> None:
        self._register_p32()
        with self.assertRaises(InvalidState):
            self.service.register_process_version(
                self.engineer, "PV-3.3", "CIS", "P3.3", PARAMS_P33, "k-p33",
                parent_version_id="PV-3.2", change_reason="调整",
            )
        self._freeze_p32()
        with self.assertRaises(ValidationFailed):
            self.service.register_process_version(
                self.engineer, "PV-3.3", "CIS", "P3.3", PARAMS_P33, "k-p33-b",
                parent_version_id="PV-3.2",
            )
        with self.assertRaises(NotFound):
            self.service.register_process_version(
                self.engineer, "PV-3.3", "CIS", "P3.3", PARAMS_P33, "k-p33-c",
                parent_version_id="PV-MISSING", change_reason="调整",
            )

    def test_missing_idempotency_key_is_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_process_version(self.engineer, "PV-3.2", "CIS", "P3.2", PARAMS_P32, "")
        with self.assertRaises(ValidationFailed):
            self.service.freeze_process_version(self.quality, "PV-3.2", "  ")

    def test_role_separation(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.register_process_version(self.operator, "PV-3.2", "CIS", "P3.2", PARAMS_P32, "k-p32")
        self._register_p32()
        with self.assertRaises(PermissionError):
            self.service.freeze_process_version(self.engineer, "PV-3.2", "k-p32-freeze")
        frozen = self.service.freeze_process_version(self.quality, "PV-3.2", "k-p32-freeze")
        self.assertEqual(frozen["state"], "frozen")

    def test_version_events_are_auditable(self) -> None:
        self._register_p32()
        self.service.update_process_params(self.engineer, "PV-3.2", PARAMS_P32, "k-upd-1")
        self._freeze_p32()
        events = self.service.version_events(self.operator, "PV-3.2")
        self.assertEqual(
            [e["event_type"] for e in events],
            ["version.registered", "version.params_updated", "version.frozen"],
        )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = PhotonService()
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = self._post("/login", {"user_id": "admin", "password": "photon-admin"})[1]["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        merged = {"Authorization": f"Bearer {getattr(self, 'token', '')}", **(headers or {})}
        connection.request(method, path, json.dumps(body) if body is not None else None, merged)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def _post(self, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
        return self._request("POST", path, body, headers)

    def test_full_flow_and_stable_error_shapes(self) -> None:
        status, created = self._post(
            "/process-versions",
            {"version_id": "PV-3.2", "product": "CIS", "version_label": "P3.2", "params": PARAMS_P32},
            {"Idempotency-Key": "api-p32"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["state"], "draft")
        # 同键重放返回相同响应
        status, replay = self._post(
            "/process-versions",
            {"version_id": "PV-3.2", "product": "CIS", "version_label": "P3.2", "params": PARAMS_P32},
            {"Idempotency-Key": "api-p32"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created, replay)
        # 同键不同内容返回稳定冲突错误
        status, conflict = self._post(
            "/process-versions",
            {"version_id": "PV-3.2", "product": "CIS", "version_label": "P3.2", "params": PARAMS_P33},
            {"Idempotency-Key": "api-p32"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "conflict")
        # 参数校验错误
        status, invalid = self._post(
            "/process-versions",
            {"version_id": "PV-BAD", "product": "CIS", "version_label": "P9", "params": {"temperature_c": -5}},
            {"Idempotency-Key": "api-bad"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(invalid["error"]["code"], "validation_failed")
        # 冻结并派生新版本
        status, frozen = self._post("/process-versions/PV-3.2/freeze", {}, {"Idempotency-Key": "api-p32-fz"})
        self.assertEqual(status, 200)
        self.assertEqual(frozen["state"], "frozen")
        # 重复冻结（换键）返回稳定状态错误
        status, twice = self._post("/process-versions/PV-3.2/freeze", {}, {"Idempotency-Key": "api-p32-fz2"})
        self.assertEqual(status, 409)
        self.assertEqual(twice["error"]["code"], "invalid_state")
        status, _ = self._post(
            "/process-versions",
            {
                "version_id": "PV-3.3", "product": "CIS", "version_label": "P3.3", "params": PARAMS_P33,
                "parent_version_id": "PV-3.2", "change_reason": "目标波长调整至 1550nm",
            },
            {"Idempotency-Key": "api-p33"},
        )
        self.assertEqual(status, 201)
        self._post("/process-versions/PV-3.3/freeze", {}, {"Idempotency-Key": "api-p33-fz"})
        # 建批次时绑定 P3.3，查询返回完整追溯链
        status, lot = self._post(
            "/lots",
            {"lot_id": "LOT-API", "product": "CIS", "process_rev": "P3.3", "wafer_count": 6, "process_version_id": "PV-3.3"},
        )
        self.assertEqual(status, 201)
        status, fetched = self._request("GET", "/lots/LOT-API")
        self.assertEqual(status, 200)
        self.assertEqual([v["version_label"] for v in fetched["process_chain"]], ["P3.3", "P3.2"])
        # 追溯链接口与版本列表
        status, chain = self._request("GET", "/process-versions/PV-3.3/chain")
        self.assertEqual(status, 200)
        self.assertEqual(len(chain["chain"]), 2)
        status, listing = self._request("GET", "/process-versions?product=CIS")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["versions"]), 2)

    def test_forbidden_for_operator_role(self) -> None:
        Handler.service.auth.create_user("op2", "op2-password", "operator")
        token = self._post("/login", {"user_id": "op2", "password": "op2-password"})[1]["token"]
        status, denied = self._request(
            "POST",
            "/process-versions",
            {"version_id": "PV-X", "product": "CIS", "version_label": "PX", "params": PARAMS_P32},
            {"Authorization": f"Bearer {token}", "Idempotency-Key": "api-denied"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
