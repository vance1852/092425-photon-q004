"""协调认证、工艺版本、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .contracts import validate_process_params
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import connect, event, transaction, utcnow, version_event


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ---- 工艺版本 ----

    @staticmethod
    def _require_key(idempotency_key: str) -> str:
        key = (idempotency_key or "").strip()
        if not key:
            raise ValidationFailed("缺少幂等键 idempotency_key")
        return key

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict | None:
        row = self.db.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: dict) -> None:
        self.db.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), utcnow()),
        )

    def _version_row(self, version_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM process_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFound(f"工艺版本不存在: {version_id}")
        return row

    def _version_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        bound_lots = self.db.execute(
            "SELECT count(*) FROM chip_lots WHERE process_version_id=?", (row["version_id"],)
        ).fetchone()[0]
        return {
            "version_id": row["version_id"],
            "product": row["product"],
            "version_label": row["version_label"],
            "parent_version_id": row["parent_version_id"],
            "change_reason": row["change_reason"],
            "params": json.loads(row["params_json"]),
            "params_sha256": row["params_sha256"],
            "state": row["state"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "frozen_by": row["frozen_by"],
            "frozen_at": row["frozen_at"],
            "bound_lots": bound_lots,
        }

    def _version_chain(self, version_id: str) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = version_id
        while current:
            if current in seen:
                break
            seen.add(current)
            row = self.db.execute("SELECT * FROM process_versions WHERE version_id=?", (current,)).fetchone()
            if row is None:
                break
            chain.append(self._version_dict(row))
            current = row["parent_version_id"]
        return chain

    def register_process_version(
        self,
        token: str,
        version_id: str,
        product: str,
        version_label: str,
        params: Any,
        idempotency_key: str,
        parent_version_id: str | None = None,
        change_reason: str = "",
    ) -> dict:
        actor = self.auth.require(token, "submit")
        key = self._require_key(idempotency_key)
        version_id = version_id.strip()
        product = product.strip()
        version_label = version_label.strip()
        parent_version_id = parent_version_id.strip() if parent_version_id else None
        change_reason = change_reason.strip()
        if not version_id or not product or not version_label:
            raise ValidationFailed("版本编号、产品和版本标签不能为空")
        if parent_version_id and not change_reason:
            raise ValidationFailed("派生版本必须填写变更原因")
        normalized = validate_process_params(params)
        request_digest = content_digest([version_id, product, version_label, parent_version_id or "", change_reason, normalized])
        replay = self._idempotent_response("process_version.register", key, request_digest)
        if replay is not None:
            return replay
        params_sha256 = content_digest([normalized])
        try:
            with transaction(self.db):
                if parent_version_id:
                    parent = self._version_row(parent_version_id)
                    if parent["state"] != "frozen":
                        raise InvalidState("父版本尚未冻结，不能作为基线派生新版本")
                self.db.execute(
                    "INSERT INTO process_versions(version_id,product,version_label,parent_version_id,change_reason,"
                    "params_json,params_sha256,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,'draft',?,?)",
                    (
                        version_id, product, version_label, parent_version_id, change_reason,
                        canonical_json(normalized), params_sha256, actor.user_id, utcnow(),
                    ),
                )
                version_event(self.db, version_id, "version.registered", actor.user_id, {
                    "product": product,
                    "version_label": version_label,
                    "parent_version_id": parent_version_id,
                    "change_reason": change_reason,
                    "params_sha256": params_sha256,
                })
                response = self._version_dict(self._version_row(version_id))
                self._store_idempotent("process_version.register", key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("工艺版本编号、产品内标签或幂等键冲突") from exc
        return response

    def update_process_params(self, token: str, version_id: str, params: Any, idempotency_key: str) -> dict:
        actor = self.auth.require(token, "submit")
        key = self._require_key(idempotency_key)
        normalized = validate_process_params(params)
        request_digest = content_digest([version_id, normalized])
        scope = f"process_version.params:{version_id}"
        replay = self._idempotent_response(scope, key, request_digest)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                row = self._version_row(version_id)
                bound = self.db.execute(
                    "SELECT count(*) FROM chip_lots WHERE process_version_id=?", (version_id,)
                ).fetchone()[0]
                if bound:
                    raise InvalidState("工艺版本已绑定批次，不能原地修改")
                if row["state"] != "draft":
                    raise InvalidState("工艺版本已冻结，不能修改参数")
                new_digest = content_digest([normalized])
                self.db.execute(
                    "UPDATE process_versions SET params_json=?,params_sha256=? WHERE version_id=? AND state='draft'",
                    (canonical_json(normalized), new_digest, version_id),
                )
                version_event(self.db, version_id, "version.params_updated", actor.user_id, {
                    "old_sha256": row["params_sha256"],
                    "new_sha256": new_digest,
                })
                response = self._version_dict(self._version_row(version_id))
                self._store_idempotent(scope, key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("幂等键并发冲突") from exc
        return response

    def freeze_process_version(self, token: str, version_id: str, idempotency_key: str) -> dict:
        actor = self.auth.require(token, "release")
        key = self._require_key(idempotency_key)
        request_digest = content_digest([version_id])
        scope = f"process_version.freeze:{version_id}"
        replay = self._idempotent_response(scope, key, request_digest)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                row = self._version_row(version_id)
                if row["state"] != "draft":
                    raise InvalidState("工艺版本已冻结，不能重复冻结")
                now = utcnow()
                cursor = self.db.execute(
                    "UPDATE process_versions SET state='frozen',frozen_by=?,frozen_at=? WHERE version_id=? AND state='draft'",
                    (actor.user_id, now, version_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("工艺版本状态已变化，冻结失败")
                version_event(self.db, version_id, "version.frozen", actor.user_id, {"params_sha256": row["params_sha256"]})
                response = self._version_dict(self._version_row(version_id))
                self._store_idempotent(scope, key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("幂等键并发冲突") from exc
        return response

    def get_process_version(self, token: str, version_id: str) -> dict:
        self.auth.require(token, "read")
        return self._version_dict(self._version_row(version_id))

    def list_process_versions(self, token: str, product: str | None = None) -> list[dict]:
        self.auth.require(token, "read")
        if product:
            rows = self.db.execute(
                "SELECT * FROM process_versions WHERE product=? ORDER BY created_at,version_id", (product,)
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM process_versions ORDER BY created_at,version_id").fetchall()
        return [self._version_dict(row) for row in rows]

    def process_version_chain(self, token: str, version_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._version_row(version_id)
        return self._version_chain(version_id)

    def version_events(self, token: str, version_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._version_row(version_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM version_events WHERE version_id=? ORDER BY event_id", (version_id,)
            ).fetchall()
        ]

    # ---- 批次 ----

    def _lot_dict(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound(f"批次不存在: {lot_id}")
        result = dict(row)
        result["process_chain"] = self._version_chain(row["process_version_id"]) if row["process_version_id"] else []
        return result

    def create_lot(
        self,
        token: str,
        lot_id: str,
        product: str,
        process_rev: str,
        wafer_count: int,
        process_version_id: str | None = None,
    ) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        try:
            with transaction(self.db):
                if process_version_id is not None:
                    version = self._version_row(process_version_id)
                    if version["state"] != "frozen":
                        raise InvalidState("工艺版本未冻结，不能绑定批次")
                self.db.execute(
                    "INSERT INTO chip_lots(lot_id,product,process_rev,wafer_count,status,owner,created_at,updated_at,"
                    "process_version_id,bound_by,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now,
                        process_version_id, actor.user_id if process_version_id else None,
                        now if process_version_id else None,
                    ),
                )
                event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
                if process_version_id is not None:
                    event(self.db, lot_id, "process_bound", actor.user_id, {
                        "process_version_id": process_version_id,
                        "version_label": version["version_label"],
                        "params_sha256": version["params_sha256"],
                    })
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"批次编号已存在: {lot_id}") from exc
        return self._lot_dict(lot_id)

    def bind_lot_version(self, token: str, lot_id: str, version_id: str, idempotency_key: str) -> dict:
        actor = self.auth.require(token, "submit")
        key = self._require_key(idempotency_key)
        request_digest = content_digest([lot_id, version_id])
        scope = f"lot.bind:{lot_id}"
        replay = self._idempotent_response(scope, key, request_digest)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                lot = self.db.execute("SELECT process_version_id FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
                if lot is None:
                    raise NotFound(f"批次不存在: {lot_id}")
                if lot["process_version_id"]:
                    raise InvalidState("批次已绑定工艺版本，不能重复绑定")
                version = self._version_row(version_id)
                if version["state"] != "frozen":
                    raise InvalidState("工艺版本未冻结，不能绑定批次")
                now = utcnow()
                cursor = self.db.execute(
                    "UPDATE chip_lots SET process_version_id=?,bound_by=?,bound_at=?,updated_at=? "
                    "WHERE lot_id=? AND process_version_id IS NULL",
                    (version_id, actor.user_id, now, now, lot_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("批次绑定状态已变化，绑定失败")
                event(self.db, lot_id, "process_bound", actor.user_id, {
                    "process_version_id": version_id,
                    "version_label": version["version_label"],
                    "params_sha256": version["params_sha256"],
                })
                response = self._lot_dict(lot_id)
                self._store_idempotent(scope, key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("幂等键并发冲突") from exc
        return response

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return self._lot_dict(lot_id)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise NotFound(f"批次不存在: {lot_id}")
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
