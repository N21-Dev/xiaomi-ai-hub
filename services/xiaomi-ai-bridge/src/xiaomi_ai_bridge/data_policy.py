"""One acquisition policy for UI hierarchy and background device reads.

Role classification belongs to the field catalog. This module consumes that
classification without treating arbitrary numeric identifiers as measurements.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

POLICY_KEYS = (
    "priority",
    "layer",
    "refresh_policy",
    "refresh_seconds",
    "cache_ttl_seconds",
)


def policy_for_field(
    field: Mapping[str, Any], primary_seconds: int | None = None
) -> dict[str, Any]:
    if primary_seconds is None:
        declared = (
            field.get("refresh_seconds") if field.get("priority") == "primary" else None
        )
        primary_seconds = declared if type(declared) is int and declared > 0 else 60
    primary_seconds = max(15, min(86400, int(primary_seconds)))
    status = field.get("status")
    if (
        field.get("sensitive")
        or status == "sensitive"
        or not field.get("readable", True)
    ):
        return {
            "priority": "detail",
            "layer": "能力与限制",
            "refresh_policy": "不自动读取",
            "refresh_seconds": 0,
            "cache_ttl_seconds": 0,
        }
    source = str(field.get("source") or "external")
    scheduled = source == "xiaomi" or source.startswith(("xiaomi.", "miot.", "miio."))
    if status == "unsupported" and scheduled:
        return {
            "priority": "detail",
            "layer": "能力与限制",
            "refresh_policy": "暂不支持，每 5 分钟重试",
            "refresh_seconds": max(300, primary_seconds),
            "cache_ttl_seconds": 0,
        }
    role = field.get("role", "metadata")
    if role in {"measurement", "counter", "state"}:
        priority, layer, seconds = "primary", "核心状态", primary_seconds
    elif role == "setting":
        priority, layer, seconds = "secondary", "常用设置", max(300, primary_seconds)
    else:
        priority, layer = "detail", "设备档案"
        seconds = max(86400 if role == "identity" else 21600, primary_seconds)
    if status == "unsupported":
        priority, layer = "detail", "能力与限制"
    if source == "mi-health-hub":
        return {
            "priority": priority,
            "layer": layer,
            "refresh_policy": "按需读取健康中心（数据时效取决于手机同步）",
            "refresh_seconds": 0,
            "cache_ttl_seconds": 0,
        }
    if not scheduled:
        declared = field.get("refresh_seconds")
        seconds = declared if type(declared) is int and 0 < declared <= 86400 else 0
        return {
            "priority": priority,
            "layer": layer,
            "refresh_policy": f"由数据源约每 {seconds} 秒上报"
            if seconds
            else "由数据源上报",
            "refresh_seconds": seconds,
            "cache_ttl_seconds": seconds * 3,
        }
    label = (
        f"每 {seconds // 86400} 天更新"
        if seconds % 86400 == 0
        else f"每 {seconds // 3600} 小时更新"
        if seconds % 3600 == 0
        else f"每 {seconds // 60} 分钟更新"
        if seconds % 60 == 0
        else f"每 {seconds} 秒更新"
    )
    return {
        "priority": priority,
        "layer": layer,
        "refresh_policy": label,
        "refresh_seconds": seconds,
        "cache_ttl_seconds": seconds * 3,
    }
