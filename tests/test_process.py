"""工艺版本登记、校验、冻结、派生、批次绑定与追溯链的服务层测试。"""

from __future__ import annotations

import unittest

from photon_fab.errors import Conflict, InvalidState, NotFound, ValidationFailed
from photon_fab.process import PARAM_SPEC
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

P33 = {**P32, "deposition_temp_c": 265.0, "rf_power_w": 330.0, "exposure_dose_mj_cm2": 240.0}


class ProcessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.auth.create_user("eng", "engineer1", "engineer")
        self.service.auth.create_user("op", "operator1", "operator")
        self.eng = self.service.auth.login("eng", "engineer1")
        self.op = self.service.auth.login("op", "operator1")

    def _register_p3(self, params=P32, key="reg-1"):
        return self.service.register_process(self.token, "P3", "CMOS image sensor", params, "P3.2 基线", key)

    def test_register_creates_draft_v1_with_snapshot(self) -> None:
        version = self._register_p3()
        self.assertEqual(version["version"], 1)
        self.assertEqual(version["state"], "draft")
        self.assertIsNone(version["parent_id"])
        self.assertEqual(version["parameters"], P32)
        self.assertEqual(len(version["parameters_sha256"]), 64)
        self.assertEqual(version["bound_lot_count"], 0)

    def test_register_validates_identifier_reason_and_parameters(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P 3", "product", P32, "reason")
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P3", "product", P32, "  ")
        missing = dict(P32)
        del missing["rf_power_w"]
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P3", "product", missing, "reason")
        unknown = {**P32, "stray_param": 1.0}
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P3", "product", unknown, "reason")
        out_of_range = {**P32, "bake_temp_c": 999.0}
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P3", "product", out_of_range, "reason")
        non_numeric = {**P32, "rf_power_w": "300"}
        with self.assertRaises(ValidationFailed):
            self.service.register_process(self.token, "P3", "product", non_numeric, "reason")
        boundary_low = {**P32, "chamber_pressure_pa": PARAM_SPEC["chamber_pressure_pa"][0]}
        ok = self.service.register_process(self.token, "P3X", "product", boundary_low, "reason")
        self.assertEqual(ok["parameters"]["chamber_pressure_pa"], 0.01)

    def test_register_requires_engineer_permission(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.register_process(self.op, "P3", "product", P32, "reason")
        self.service.register_process(self.eng, "P3E", "product", P32, "reason")

    def test_register_without_key_twice_conflicts(self) -> None:
        self.service.register_process(self.token, "P3", "product", P32, "reason")
        with self.assertRaises(Conflict):
            self.service.register_process(self.token, "P3", "product", P32, "reason")

    def test_freeze_transition_and_double_freeze(self) -> None:
        self._register_p3()
        frozen = self.service.freeze_process(self.token, "P3", 1)
        self.assertEqual(frozen["state"], "frozen")
        self.assertIsNotNone(frozen["frozen_at"])
        with self.assertRaises(InvalidState) as ctx:
            self.service.freeze_process(self.token, "P3", 1)
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_freeze_unknown_version_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.freeze_process(self.token, "P3", 9)

    def test_frozen_version_cannot_be_updated_in_place(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        with self.assertRaises(InvalidState):
            self.service.update_process(self.token, "P3", 1, P33, "想直接改发布版")

    def test_draft_update_persists_before_freeze(self) -> None:
        self._register_p3()
        updated = self.service.update_process(self.token, "P3", 1, P33, "试产微调")
        self.assertEqual(updated["parameters"]["rf_power_w"], 330.0)
        self.assertEqual(updated["change_reason"], "试产微调")
        events = self.service.db.execute(
            "SELECT event_type FROM process_events WHERE process_id='P3' ORDER BY event_id"
        ).fetchall()
        self.assertEqual([r[0] for r in events], ["process.registered", "process.updated"])

    def test_update_validates_parameters(self) -> None:
        self._register_p3()
        bad = {**P32, "etch_time_s": -1.0}
        with self.assertRaises(ValidationFailed):
            self.service.update_process(self.token, "P3", 1, bad, "x")

    def test_derive_requires_frozen_parent_and_chains(self) -> None:
        self._register_p3()
        with self.assertRaises(InvalidState):
            self.service.derive_process(self.token, "P3", 1, P33, "P3.3 变更")
        self.service.freeze_process(self.token, "P3", 1)
        with self.assertRaises(NotFound):
            self.service.derive_process(self.token, "P3", 9, P33, "P3.3 变更")
        derived = self.service.derive_process(self.token, "P3", 1, P33, "P3.3 变更", idempotency_key="d1")
        self.assertEqual(derived["version"], 2)
        self.assertEqual(derived["parent_id"], "P3")
        self.assertEqual(derived["parent_version"], 1)
        self.assertEqual(derived["state"], "draft")
        self.assertEqual(derived["product"], "CMOS image sensor")
        # 父版本快照保持不变。
        parent = self.service.get_process_version(self.token, "P3", 1)
        self.assertEqual(parent["parameters"], P32)
        self.assertEqual(parent["state"], "frozen")

    def test_derive_to_new_process_line_keeps_parent_link(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        branch = self.service.derive_process(
            self.token, "P3", 1, P33, "分支试产", new_process_id="P3-SPECIAL"
        )
        self.assertEqual(branch["process_id"], "P3-SPECIAL")
        self.assertEqual(branch["version"], 1)
        self.assertEqual(branch["parent_id"], "P3")
        self.assertEqual(branch["parent_version"], 1)

    def test_list_versions_orders_by_version(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        self.service.derive_process(self.token, "P3", 1, P33, "P3.3")
        versions = self.service.list_process_versions(self.token, "P3")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        with self.assertRaises(NotFound):
            self.service.list_process_versions(self.token, "UNKNOWN")

    def test_idempotent_register_and_derive_replay(self) -> None:
        first = self._register_p3()
        second = self._register_p3()
        self.assertEqual(first, second)
        count = self.service.db.execute("SELECT count(*) FROM process_versions WHERE process_id='P3'").fetchone()[0]
        self.assertEqual(count, 1)
        self.service.freeze_process(self.token, "P3", 1)
        d1 = self.service.derive_process(self.token, "P3", 1, P33, "P3.3", idempotency_key="d1")
        d2 = self.service.derive_process(self.token, "P3", 1, P33, "P3.3", idempotency_key="d1")
        self.assertEqual(d1, d2)
        count = self.service.db.execute("SELECT count(*) FROM process_versions WHERE process_id='P3'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_same_idempotency_key_different_payload_conflicts(self) -> None:
        self._register_p3(key="shared")
        with self.assertRaises(Conflict):
            self.service.register_process(self.token, "P3", "CMOS image sensor", P33, "不同参数", "shared")

    def test_bind_requires_frozen_and_is_immutable(self) -> None:
        self._register_p3()
        self.service.create_lot(self.token, "LOT-A", "CMOS image sensor", "draft", 6)
        with self.assertRaises(InvalidState):
            self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "试产")
        self.service.freeze_process(self.token, "P3", 1)
        binding = self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "P3.2 试产", "bind-1")
        self.assertEqual(binding["process_id"], "P3")
        self.assertEqual(binding["version"], 1)
        with self.assertRaises(InvalidState):
            self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "重复绑定")
        # 批次 process_rev 同步为实际绑定版本。
        self.assertEqual(self.service.get_lot(self.token, "LOT-A")["process_rev"], "P3-v1")
        bound = self.service.get_process_version(self.token, "P3", 1)
        self.assertEqual(bound["bound_lot_count"], 1)

    def test_bind_idempotent_replay_and_missing_entities(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        self.service.create_lot(self.token, "LOT-A", "product", "rev", 6)
        b1 = self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "试产", "k")
        b2 = self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "试产", "k")
        self.assertEqual(b1, b2)
        with self.assertRaises(NotFound):
            self.service.bind_lot_process(self.token, "LOT-MISSING", "P3", 1)
        self.service.create_lot(self.token, "LOT-B", "product", "rev", 6)
        with self.assertRaises(NotFound):
            self.service.bind_lot_process(self.token, "LOT-B", "P3", 99)

    def test_parallel_p32_p33_lots_have_independent_trace_chains(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        p33 = self.service.derive_process(self.token, "P3", 1, P33, "P3.3 提温", idempotency_key="d")
        self.service.freeze_process(self.token, "P3", 2)
        self.service.create_lot(self.token, "LOT-P32", "product", "P3-v1", 10)
        self.service.create_lot(self.token, "LOT-P33", "product", "P3-v2", 8)
        self.service.bind_lot_process(self.token, "LOT-P32", "P3", 1, "P3.2 批")
        self.service.bind_lot_process(self.token, "LOT-P33", "P3", 2, "P3.3 批")

        trace32 = self.service.lot_trace(self.token, "LOT-P32")
        self.assertEqual([(v["process_id"], v["version"]) for v in trace32["lineage"]], [("P3", 1)])

        trace33 = self.service.lot_trace(self.token, "LOT-P33")
        chain = [(v["process_id"], v["version"], v["change_reason"]) for v in trace33["lineage"]]
        self.assertEqual(chain, [("P3", 2, "P3.3 提温"), ("P3", 1, "P3.2 基线")])
        self.assertEqual(trace33["bound_version"]["parameters"], P33)
        self.assertEqual(trace33["lineage"][-1]["parameters"], P32)
        event_types = {e["event_type"] for e in trace33["process_events"]}
        self.assertIn("process.frozen", event_types)
        self.assertIn("process.bound", event_types)
        self.assertTrue(any(e["event_type"] == "process_bound" for e in trace33["lot_events"]))

    def test_trace_unknown_and_unbound_lot(self) -> None:
        with self.assertRaises(NotFound):
            self.service.lot_trace(self.token, "NO-LOT")
        self.service.create_lot(self.token, "LOT-X", "product", "rev", 1)
        with self.assertRaises(NotFound):
            self.service.lot_trace(self.token, "LOT-X")

    def test_history_survives_new_releases(self) -> None:
        self._register_p3()
        self.service.freeze_process(self.token, "P3", 1)
        self.service.create_lot(self.token, "LOT-A", "product", "P3-v1", 4)
        self.service.bind_lot_process(self.token, "LOT-A", "P3", 1, "基线批")
        self.service.derive_process(self.token, "P3", 1, P33, "P3.3")
        self.service.freeze_process(self.token, "P3", 2)
        # 历史批次仍读取到冻结时的 v1 快照与追溯链。
        trace = self.service.lot_trace(self.token, "LOT-A")
        self.assertEqual(trace["binding"]["version"], 1)
        self.assertEqual(trace["lineage"][0]["parameters"], P32)


if __name__ == "__main__":
    unittest.main()
