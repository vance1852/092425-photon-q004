"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService

PARAMS_P32 = {
    "temperature_c": 850.0,
    "pressure_pa": 133.3,
    "duration_min": 120.0,
    "gas_flow_sccm": 500.0,
    "target_wavelength_nm": 1310.0,
}
PARAMS_P33 = dict(PARAMS_P32, temperature_c=870.0, duration_min=135.0, target_wavelength_nm=1550.0)


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.register_process_version(token, "PV-CIS-3.2", "CMOS image sensor", "P3.2", PARAMS_P32, "acc-pv32")
    service.freeze_process_version(token, "PV-CIS-3.2", "acc-pv32-freeze")
    service.register_process_version(
        token, "PV-CIS-3.3", "CMOS image sensor", "P3.3", PARAMS_P33, "acc-pv33",
        parent_version_id="PV-CIS-3.2", change_reason="目标波长调整至 1550nm 并延长退火",
    )
    service.freeze_process_version(token, "PV-CIS-3.3", "acc-pv33-freeze")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.3", 10, process_version_id="PV-CIS-3.3")
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    lot = service.get_lot(token, "LOT-DEMO")
    chain = [version["version_label"] for version in lot["process_chain"]]
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(token, "LOT-DEMO")),
        "process_chain": chain,
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
