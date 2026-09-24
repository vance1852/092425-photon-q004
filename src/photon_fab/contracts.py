"""工艺版本参数的契约校验。"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .errors import ValidationFailed

# 参数名 -> (下限, 上限, 下限是否可取)。所有上限均为闭区间。
PARAMETER_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "temperature_c": (0.0, 1200.0, True),
    "pressure_pa": (0.0, 200000.0, False),
    "duration_min": (0.0, 1440.0, False),
    "gas_flow_sccm": (0.0, 10000.0, True),
    "target_wavelength_nm": (200.0, 20000.0, True),
}


def validate_process_params(raw: Any) -> dict[str, float]:
    """校验并规范化一组工艺参数；非法输入抛出 ValidationFailed。"""

    if not isinstance(raw, Mapping):
        raise ValidationFailed("工艺参数必须是 JSON 对象")
    unknown = sorted(set(raw) - set(PARAMETER_BOUNDS))
    if unknown:
        raise ValidationFailed(f"未知工艺参数: {', '.join(unknown)}")
    missing = sorted(set(PARAMETER_BOUNDS) - set(raw))
    if missing:
        raise ValidationFailed(f"缺少工艺参数: {', '.join(missing)}")
    normalized: dict[str, float] = {}
    for name, (low, high, low_inclusive) in PARAMETER_BOUNDS.items():
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationFailed(f"工艺参数 {name} 必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise ValidationFailed(f"工艺参数 {name} 必须是有限数值")
        below = number < low if low_inclusive else number <= low
        if below or number > high:
            bracket = "[" if low_inclusive else "("
            raise ValidationFailed(f"工艺参数 {name} 超出范围 {bracket}{low}, {high}]")
        normalized[name] = number
    return normalized
