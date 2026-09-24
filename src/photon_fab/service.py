"""协调认证、批次、测试、放行门禁与工艺版本管理的应用服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .process import (
    DRAFT,
    FROZEN,
    parameters_digest,
    request_digest,
    require_identifier,
    require_name,
    validate_parameters,
)
from .storage import connect, event, process_event, transaction, utcnow


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 工艺版本登记 / 校验 / 冻结 / 派生
    # ------------------------------------------------------------------ #

    def register_process(
        self,
        token: str,
        process_id: str,
        product: str,
        parameters: Mapping[str, Any],
        change_reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """登记一个新工艺的首个草稿版本（version=1, state=draft）。"""
        actor = self.auth.require(token, "process_manage")
        process_id = require_identifier(process_id, "process_id")
        product = require_name(product, "product")
        normalized = validate_parameters(parameters)
        reason = (change_reason or "").strip()
        if not reason:
            raise ValidationFailed("change_reason 不能为空")
        payload = {
            "process_id": process_id,
            "product": product,
            "parameters": normalized,
            "change_reason": reason,
        }
        return self._idempotent(
            "process.register", idempotency_key, payload,
            lambda: self._insert_process(actor.user_id, process_id, None, None, product, normalized, reason),
        )

    def _insert_process(
        self, actor_id: str, process_id: str, parent_id: str | None, parent_version: int | None,
        product: str | None, parameters: dict[str, float], reason: str,
    ) -> dict:
        digest = parameters_digest(parameters)
        now = utcnow()
        try:
            with transaction(self.db):
                if parent_id is None:
                    exists = self.db.execute(
                        "SELECT 1 FROM process_versions WHERE process_id=?", (process_id,)
                    ).fetchone()
                    if exists:
                        raise Conflict(f"工艺 {process_id} 已存在，新版本应通过派生发布")
                    version = 1
                    assert product is not None
                else:
                    parent = self.db.execute(
                        "SELECT state,product FROM process_versions WHERE process_id=? AND version=?",
                        (parent_id, parent_version),
                    ).fetchone()
                    if parent is None:
                        raise NotFound("父工艺版本不存在")
                    if parent["state"] != FROZEN:
                        raise InvalidState("只能从已冻结的父版本派生新版本")
                    # 产品名沿父版本继承，避免同产品并行试产时标识与名称错位。
                    product = parent["product"]
                    row = self.db.execute(
                        "SELECT coalesce(max(version),0)+1 FROM process_versions WHERE process_id=?",
                        (process_id,),
                    ).fetchone()
                    version = row[0]
                self.db.execute(
                    "INSERT INTO process_versions(process_id,version,parent_id,parent_version,state,"
                    "product,parameters_json,parameters_sha256,change_reason,created_by,created_at,frozen_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (process_id, version, parent_id, parent_version, DRAFT, product,
                     json.dumps(parameters, ensure_ascii=False, sort_keys=True), digest,
                     reason, actor_id, now, None),
                )
                event_type = "process.registered" if parent_id is None else "process.derived"
                process_event(self.db, process_id, version, event_type, actor_id, {
                    "parent_id": parent_id, "parent_version": parent_version,
                    "change_reason": reason, "parameters_sha256": digest,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("工艺版本冲突或父版本不存在") from exc
        return self._process_view(process_id, version)

    def get_process_version(self, token: str, process_id: str, version: int) -> dict:
        self.auth.require(token, "read")
        return self._process_view(process_id, version)

    def _process_view(self, process_id: str, version: int) -> dict:
        row = self.db.execute(
            "SELECT pv.*, ("
            "SELECT count(*) FROM lot_process_bindings b WHERE b.process_id=pv.process_id AND b.version=pv.version"
            ") AS bound_lot_count FROM process_versions pv WHERE pv.process_id=? AND pv.version=?",
            (process_id, int(version)),
        ).fetchone()
        if row is None:
            raise NotFound("工艺版本不存在")
        return self._view_from_row(row)

    @staticmethod
    def _view_from_row(row: sqlite3.Row) -> dict:
        return {
            "process_id": row["process_id"],
            "version": row["version"],
            "state": row["state"],
            "product": row["product"],
            "parent_id": row["parent_id"],
            "parent_version": row["parent_version"],
            "parameters": json.loads(row["parameters_json"]),
            "parameters_sha256": row["parameters_sha256"],
            "change_reason": row["change_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "frozen_at": row["frozen_at"],
            "bound_lot_count": row["bound_lot_count"],
        }

    def list_process_versions(self, token: str, process_id: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT pv.*, (SELECT count(*) FROM lot_process_bindings b WHERE b.process_id=pv.process_id "
            "AND b.version=pv.version) AS bound_lot_count FROM process_versions pv "
            "WHERE pv.process_id=? ORDER BY pv.version",
            (process_id,),
        ).fetchall()
        if not rows:
            raise NotFound("工艺不存在")
        return [self._view_from_row(row) for row in rows]

    def update_process(
        self, token: str, process_id: str, version: int, parameters: Mapping[str, Any], change_reason: str
    ) -> dict:
        """原地修改仅限未冻结且尚未绑定任何批次的草稿版本。"""
        actor = self.auth.require(token, "process_manage")
        process_id = require_identifier(process_id, "process_id")
        normalized = validate_parameters(parameters)
        reason = (change_reason or "").strip()
        if not reason:
            raise ValidationFailed("change_reason 不能为空")
        digest = parameters_digest(normalized)
        with transaction(self.db):
            row = self.db.execute(
                "SELECT state FROM process_versions WHERE process_id=? AND version=?",
                (process_id, int(version)),
            ).fetchone()
            if row is None:
                raise NotFound("工艺版本不存在")
            if row["state"] != DRAFT:
                raise InvalidState("工艺版本已冻结，只能通过派生新版本发布变更")
            bound = self.db.execute(
                "SELECT 1 FROM lot_process_bindings WHERE process_id=? AND version=?",
                (process_id, int(version)),
            ).fetchone()
            if bound:
                raise InvalidState("工艺版本已绑定批次，参数不可原地修改")
            self.db.execute(
                "UPDATE process_versions SET parameters_json=?, parameters_sha256=?, change_reason=? "
                "WHERE process_id=? AND version=?",
                (json.dumps(normalized, ensure_ascii=False, sort_keys=True), digest, reason,
                 process_id, int(version)),
            )
            process_event(self.db, process_id, int(version), "process.updated", actor.user_id, {
                "change_reason": reason, "parameters_sha256": digest,
            })
        return self._process_view(process_id, int(version))

    def freeze_process(self, token: str, process_id: str, version: int) -> dict:
        """发布工艺版本：draft -> frozen，冻结后参数成为不可变历史快照。"""
        actor = self.auth.require(token, "process_manage")
        process_id = require_identifier(process_id, "process_id")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT state FROM process_versions WHERE process_id=? AND version=?",
                (process_id, int(version)),
            ).fetchone()
            if row is None:
                raise NotFound("工艺版本不存在")
            if row["state"] == FROZEN:
                raise InvalidState("工艺版本已冻结，不能重复冻结")
            if row["state"] != DRAFT:
                raise InvalidState(f"非法状态转换: {row['state']} -> frozen")
            now = utcnow()
            self.db.execute(
                "UPDATE process_versions SET state=?, frozen_at=? WHERE process_id=? AND version=?",
                (FROZEN, now, process_id, int(version)),
            )
            process_event(self.db, process_id, int(version), "process.frozen", actor.user_id, {"frozen_at": now})
        return self._process_view(process_id, int(version))

    def derive_process(
        self,
        token: str,
        parent_id: str,
        parent_version: int,
        parameters: Mapping[str, Any],
        change_reason: str,
        new_process_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """从已冻结父版本发布新版本；不传 new_process_id 时在同一工艺线上递增版本号。"""
        actor = self.auth.require(token, "process_manage")
        parent_id = require_identifier(parent_id, "parent_id")
        target_id = require_identifier(new_process_id, "new_process_id") if new_process_id else parent_id
        normalized = validate_parameters(parameters)
        reason = (change_reason or "").strip()
        if not reason:
            raise ValidationFailed("change_reason 不能为空")
        payload = {
            "parent_id": parent_id,
            "parent_version": int(parent_version),
            "new_process_id": target_id,
            "parameters": normalized,
            "change_reason": reason,
        }
        return self._idempotent(
            "process.derive", idempotency_key, payload,
            lambda: self._insert_process(actor.user_id, target_id, parent_id, int(parent_version),
                                         None, normalized, reason),
        )

    # ------------------------------------------------------------------ #
    # 批次绑定与追溯链
    # ------------------------------------------------------------------ #

    def bind_lot_process(
        self,
        token: str,
        lot_id: str,
        process_id: str,
        version: int,
        change_reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """将批次绑定到一个已冻结的工艺版本；绑定后版本快照即批次实际采用参数。"""
        actor = self.auth.require(token, "submit")
        lot_id = require_identifier(lot_id, "lot_id")
        process_id = require_identifier(process_id, "process_id")
        version = int(version)
        reason = (change_reason or "").strip()
        payload = {"lot_id": lot_id, "process_id": process_id, "version": version, "change_reason": reason}

        def execute() -> dict:
            with transaction(self.db):
                lot = self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
                if lot is None:
                    raise NotFound("批次不存在")
                existing = self.db.execute(
                    "SELECT process_id,version FROM lot_process_bindings WHERE lot_id=?", (lot_id,)
                ).fetchone()
                if existing is not None:
                    raise InvalidState("批次已绑定工艺版本，绑定不可更改")
                process_row = self.db.execute(
                    "SELECT state FROM process_versions WHERE process_id=? AND version=?",
                    (process_id, version),
                ).fetchone()
                if process_row is None:
                    raise NotFound("工艺版本不存在")
                if process_row["state"] != FROZEN:
                    raise InvalidState("只能绑定已冻结的工艺版本")
                now = utcnow()
                self.db.execute(
                    "INSERT INTO lot_process_bindings(lot_id,process_id,version,bound_by,bound_at,change_reason)"
                    " VALUES(?,?,?,?,?,?)",
                    (lot_id, process_id, version, actor.user_id, now, reason),
                )
                self.db.execute(
                    "UPDATE chip_lots SET process_rev=?, updated_at=? WHERE lot_id=?",
                    (f"{process_id}-v{version}", now, lot_id),
                )
                process_event(self.db, process_id, version, "process.bound", actor.user_id, {
                    "lot_id": lot_id, "change_reason": reason,
                })
                event(self.db, lot_id, "process_bound", actor.user_id, {
                    "process_id": process_id, "version": version, "change_reason": reason,
                })
            return self._binding_view(lot_id)

        return self._idempotent("process.bind", idempotency_key, payload, execute)

    def _binding_view(self, lot_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM lot_process_bindings WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("批次尚未绑定工艺版本")
        return dict(row)

    def get_lot_process(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return self._binding_view(lot_id)

    def lot_trace(self, token: str, lot_id: str) -> dict:
        """返回批次绑定版本的完整父版本追溯链（当前版本在前，根版本在后）。"""
        self.auth.require(token, "read")
        lot_row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot_row is None:
            raise NotFound("批次不存在")
        binding = self.db.execute(
            "SELECT * FROM lot_process_bindings WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if binding is None:
            raise NotFound("批次尚未绑定工艺版本")
        lineage: list[dict] = []
        process_id, process_version = binding["process_id"], binding["version"]
        while True:
            row = self.db.execute(
                "SELECT pv.*, (SELECT count(*) FROM lot_process_bindings b WHERE b.process_id=pv.process_id "
                "AND b.version=pv.version) AS bound_lot_count FROM process_versions pv "
                "WHERE pv.process_id=? AND pv.version=?",
                (process_id, process_version),
            ).fetchone()
            if row is None:
                break
            lineage.append(self._view_from_row(row))
            if row["parent_id"] is None:
                break
            process_id, process_version = row["parent_id"], row["parent_version"]
        process_timeline = [
            dict(r) for r in self.db.execute(
                "SELECT event_id,process_id,version,event_type,actor,payload,created_at "
                "FROM process_events WHERE process_id=? ORDER BY event_id", (binding["process_id"],)
            ).fetchall()
        ]
        for item in process_timeline:
            item["payload"] = json.loads(item["payload"])
        return {
            "lot": dict(lot_row),
            "binding": dict(binding),
            "bound_version": lineage[0] if lineage else None,
            "lineage": lineage,
            "process_events": process_timeline,
            "lot_events": self.audit(token, lot_id),
        }

    # ------------------------------------------------------------------ #
    # 幂等支撑
    # ------------------------------------------------------------------ #

    def _idempotent(self, scope: str, key: str | None, payload: dict, operation) -> dict:
        digest = request_digest(payload)
        if key is None:
            return operation()
        cached = self._stored_response(scope, key, digest)
        if cached is not None:
            return cached
        try:
            response = operation()
        except (Conflict, InvalidState):
            # 并发重复提交时，赢家可能已先提交并记录响应；此时回放同一结果而非报错。
            cached = self._stored_response(scope, key, digest)
            if cached is not None:
                return cached
            raise
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO process_idempotency(scope,idempotency_key,request_sha256,response_json,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (scope, key, digest, json.dumps(response, ensure_ascii=False, sort_keys=True), utcnow()),
                )
        except sqlite3.IntegrityError:
            cached = self._stored_response(scope, key, digest)
            if cached is not None:
                return cached
            raise Conflict("同一幂等键对应了不同请求内容")
        return response

    def _stored_response(self, scope: str, key: str, digest: str) -> dict | None:
        with transaction(self.db):
            row = self.db.execute(
                "SELECT request_sha256,response_json FROM process_idempotency WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    # ------------------------------------------------------------------ #
    # 既有批次 / 测量 / 审批能力
    # ------------------------------------------------------------------ #

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
