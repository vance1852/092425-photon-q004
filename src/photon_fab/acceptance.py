"""容器和快照检查使用的冒烟验收命令。

除既有批次测量流程外，覆盖 P3.2/P3.3 并行试产场景：
登记工艺 -> 冻结发布 -> 绑定批次 -> 派生新版本 -> 查询完整追溯链，
并验证重复提交幂等与非法状态转换的稳定错误码。
"""

from __future__ import annotations

import argparse
import json

from .errors import InvalidState
from .service import PhotonService

P32_PARAMS = {
    "deposition_temp_c": 250.0,
    "chamber_pressure_pa": 5.0,
    "gas_flow_sccm": 120.0,
    "rf_power_w": 300.0,
    "etch_time_s": 45.0,
    "bake_temp_c": 120.0,
    "exposure_dose_mj_cm2": 220.0,
}

P33_PARAMS = {
    **P32_PARAMS,
    "deposition_temp_c": 265.0,
    "rf_power_w": 330.0,
    "exposure_dose_mj_cm2": 240.0,
}


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")

    # P3.2 首版登记、冻结，绑定到并行试产批次 LOT-P32。
    registered = service.register_process(token, "P3", "CMOS image sensor", P32_PARAMS, "P3.2 基线工艺", "reg-p3")
    assert registered["version"] == 1 and registered["state"] == "draft"
    # 幂等：同一幂等键重复提交返回同一版本，不产生第二条记录。
    replayed = service.register_process(token, "P3", "CMOS image sensor", P32_PARAMS, "P3.2 基线工艺", "reg-p3")
    assert replayed == registered
    frozen = service.freeze_process(token, "P3", 1)
    assert frozen["state"] == "frozen"

    service.create_lot(token, "LOT-P32", "CMOS image sensor", "P3-v1", 10)
    service.bind_lot_process(token, "LOT-P32", "P3", 1, "P3.2 试产批", "bind-p32")
    service.bind_lot_process(token, "LOT-P32", "P3", 1, "P3.2 试产批", "bind-p32")  # 幂等重放

    # 已绑定批次的版本不可原地修改。
    try:
        service.update_process(token, "P3", 1, P32_PARAMS, "尝试改已发布版本")
    except InvalidState as exc:
        assert exc.code == "invalid_state"
        update_blocked = True
    else:
        update_blocked = False
    assert update_blocked

    # P3.3 作为新版本从已冻结父版本派生，保留父版本与变更原因。
    p33 = service.derive_process(token, "P3", 1, P33_PARAMS, "P3.3：提升沉积温度与曝光剂量", idempotency_key="derive-p33")
    assert p33["version"] == 2 and p33["parent_version"] == 1
    service.freeze_process(token, "P3", 2)
    service.create_lot(token, "LOT-P33", "CMOS image sensor", "P3-v2", 8)
    service.bind_lot_process(token, "LOT-P33", "P3", 2, "P3.3 并行试产批")

    # 查询批次返回完整追溯链（P3.3 -> P3.2）。
    trace = service.lot_trace(token, "LOT-P33")
    lineage = [(item["process_id"], item["version"]) for item in trace["lineage"]]
    assert lineage == [("P3", 2), ("P3", 1)], lineage
    assert trace["lineage"][0]["parameters"]["deposition_temp_c"] == 265.0
    assert trace["lineage"][1]["parameters"]["deposition_temp_c"] == 250.0
    assert trace["lineage"][0]["change_reason"].startswith("P3.3")

    # 重复冻结返回稳定的非法状态错误。
    try:
        service.freeze_process(token, "P3", 2)
    except InvalidState as exc:
        refreeze_blocked = exc.code == "invalid_state"
    else:
        refreeze_blocked = False
    assert refreeze_blocked

    # 既有测量 / 分析 / 审批流程保持可用。
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3-v1", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")

    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(token, "LOT-DEMO")),
        "process_versions": len(service.list_process_versions(token, "P3")),
        "trace_chain": lineage,
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
