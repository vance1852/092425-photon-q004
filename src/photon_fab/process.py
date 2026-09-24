"""工艺版本的领域规则：状态机、参数规格与取值校验。

P3.2/P3.3 并行试产时，每个工艺版本登记一组完整的确定性工艺参数快照；
只有 ``draft`` 版本可以修改参数，冻结（``frozen``）后只允许作为新版本
的父版本被派生引用，绝不允许原地修改。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .errors import ValidationFailed

DRAFT = "draft"
FROZEN = "frozen"

# 参数名 -> (下限, 上限, 单位)。所有取值均为闭区间，含端点。
PARAM_SPEC: dict[str, tuple[float, float, str]] = {
    "deposition_temp_c": (20.0, 500.0, "C"),
    "chamber_pressure_pa": (0.01, 100000.0, "Pa"),
    "gas_flow_sccm": (0.0, 5000.0, "sccm"),
    "rf_power_w": (0.0, 5000.0, "W"),
    "etch_time_s": (0.0, 7200.0, "s"),
    "bake_temp_c": (20.0, 400.0, "C"),
    "exposure_dose_mj_cm2": (0.0, 1000.0, "mJ/cm^2"),
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def require_identifier(value: str, field: str) -> str:
    text = str(value).strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ValidationFailed(f"{field} 必须为 1-64 位字母、数字或 ._- 且以字母数字开头")
    return text


def require_name(value: str, field: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 128:
        raise ValidationFailed(f"{field} 必须为 1-128 个字符的非空文本")
    return text


def validate_parameters(raw: Mapping[str, Any]) -> dict[str, float]:
    """校验参数载荷：必须完整提供规格中的每一项，取值为闭区间内的有限数。"""
    if not isinstance(raw, Mapping):
        raise ValidationFailed("parameters 必须是 JSON 对象")
    normalized: dict[str, float] = {}
    unknown = sorted(set(raw) - set(PARAM_SPEC))
    if unknown:
        raise ValidationFailed(f"存在未定义的工艺参数: {', '.join(unknown)}")
    for name, (low, high, unit) in PARAM_SPEC.items():
        if name not in raw:
            raise ValidationFailed(f"缺少必填工艺参数: {name}")
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationFailed(f"工艺参数 {name} 必须是数值")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValidationFailed(f"工艺参数 {name} 必须是有限数值")
        if not low <= number <= high:
            raise ValidationFailed(f"工艺参数 {name}={number:g} 超出允许区间 [{low:g}, {high:g}] {unit}")
        normalized[name] = number
    return normalized


def request_digest(payload: Any) -> str:
    """对请求载荷计算规范化 SHA-256，用于幂等冲突判定。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parameters_digest(parameters: Mapping[str, float]) -> str:
    return request_digest(parameters)
