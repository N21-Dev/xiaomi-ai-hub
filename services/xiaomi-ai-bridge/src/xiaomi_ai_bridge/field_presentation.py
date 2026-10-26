"""Derived display semantics, independent of sampling and control permissions.

The catalog owns role, sensitivity, and analysis eligibility. Presentation only
chooses where a field appears and how its existing value should be formatted.
It never converts a value or guesses a unit from a property name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CURRENT_COUNTS = {
    "active-threads", "connected-device-count", "connected-device-number",
    "online-device-count", "active-count", "connection-count",
}
_TOTAL_COUNTS = {"known-device-count", "total-threads", "total-count"}
_CUMULATIVE_COUNTS = {
    "steps", "step-count", "error-count", "completed-threads",
    "input-tokens", "output-tokens", "total-tokens", "prompt-tokens",
    "completion-tokens", "cached-tokens", "reasoning-tokens", "token-count",
    "tokens-used", "wan-download-bytes", "wan-upload-bytes",
    "power-consumption", "filter-used-flow",
}
_RUNNING_DURATIONS = {
    "uptime", "uptime-seconds", "wan-uptime-seconds", "working-time",
    "running-time", "runtime", "runtime-seconds",
}
_MEASURED_DURATIONS = {"duration", "sleep-duration", "sleep-total", "sleep-total-minutes"}
_TIME_UNITS = {"秒", "分钟", "小时", "天", "s", "ms", "min", "h", "d", "毫秒"}
_COUNTDOWNS = {
    "countdown", "countdown-time", "delay-off-countdown", "work-countdown-sec",
    "break-countdown-sec", "off-delay-time", "on-delay-time",
}
_USAGE_FIELDS = {
    "progress", "battery-level", "battery-power", "cpu-percent",
    "memory-percent", "disk-percent", "filter-life-level", "filter-life",
    "filter-left-time", "filter-used-time", "filter-hours-used",
    "filter-used-flow", "filter-left-flow", "power-consumption",
    "calories", "distance", "steps", "step-count",
}
_TIMESTAMP = re.compile(
    r"(^|[-_])(timestamp|date)$|(^|[-_])(created|updated|observed|received|attempted|started|ended|finished|reported|sampled)-at$|^last-seen$"
)


def _semantic(field: Mapping[str, Any]) -> str:
    return str(field.get("semantic") or "").lower().replace("_", "-")


def semantic_role_for_field(field: Mapping[str, Any]) -> str | None:
    """Correct only recognized numeric semantics after permission/ID checks.

    These corrections keep the same primary acquisition tier. Callers must
    apply identity, sensitivity, writability, and explicit event rules first.
    """
    if field.get("type") != "number":
        return None
    semantic = _semantic(field)
    if semantic in _CURRENT_COUNTS | _TOTAL_COUNTS:
        return "counter"
    if semantic in _RUNNING_DURATIONS | _COUNTDOWNS:
        return "state"
    return None


def presentation_for_field(field: Mapping[str, Any]) -> dict[str, str]:
    """Return additive display metadata from an already classified field."""
    semantic = _semantic(field)
    role = field.get("role", "metadata")
    field_type = field.get("type", "unknown")
    unit = field.get("unit", "")

    def result(group: str, kind: str, label: str) -> dict[str, str]:
        return {"group": group, "kind": kind, "label": label}

    # Identity and redaction win over plausible units or incoming display hints.
    if field.get("sensitive") or field.get("status") == "sensitive":
        return result("profile", "text", "敏感字段")
    if field_type in {"array", "object"} or role == "collection":
        return result("records", "collection", "结构数据")
    if role == "identity":
        presentation = result("profile", "identifier", "标识")
        # JSON numbers can exceed the browser's exact integer range. Keep the
        # raw value intact and provide a display string only for identifiers.
        if type(field.get("value")) is int:
            presentation["text"] = str(field["value"])
        return presentation
    if role == "event":
        return result("records", "event", "事件")
    if role == "parameter":
        return result("profile", "text", "操作参数")
    if role == "metadata":
        if _TIMESTAMP.search(semantic):
            return result("profile", "timestamp", "时间")
        # Scalar entries in a plain list belong to that record, not the device
        # profile. Explicitly typed measurements in records remain measurable.
        if any(type(part) is int for part in field.get("path", [])):
            return result("records", "text", "记录内容")
        return result("profile", "text", "档案信息")

    if field_type == "boolean":
        kind, label = "boolean", "开关设置" if role == "setting" else "开关状态"
    elif field_type == "enum":
        kind, label = "enum", "选项设置" if role == "setting" else "枚举状态"
    elif semantic in _COUNTDOWNS:
        kind, label = "countdown", "倒计时"
    elif semantic in _RUNNING_DURATIONS:
        kind, label = "duration", "运行时长"
    elif semantic in _MEASURED_DURATIONS and unit in _TIME_UNITS:
        kind, label = "duration", "时长"
    elif semantic in {"filter-used-time", "filter-hours-used"}:
        kind, label = "duration", "累计时长"
    elif semantic == "filter-left-time":
        kind, label = "duration", "剩余时长"
    elif field_type == "number" and unit in {"%", "percent", "percentage"}:
        kind, label = "percentage", "百分比"
    elif semantic in _CURRENT_COUNTS:
        kind, label = "current_count", "当前数量"
    elif semantic in _TOTAL_COUNTS:
        kind, label = "total_count", "数量总计"
    elif semantic in _CUMULATIVE_COUNTS:
        kind, label = "cumulative_count", "累计用量"
    elif role == "counter":
        # A counter may decrease (current jobs, clients, stock). Accumulation
        # is shown only when the semantic declares it, never from role alone.
        kind, label = "current_count", "数量"
    elif field_type == "number":
        kind, label = "number", "数值"
    else:
        kind, label = "text", "文本"

    if role == "setting":
        group = "settings"
    elif role == "state":
        group = "states"
    elif semantic in _USAGE_FIELDS or role == "counter":
        group = "usage"
    elif role == "measurement":
        group = "measurements"
    else:
        group = "profile"
    return result(group, kind, label)
