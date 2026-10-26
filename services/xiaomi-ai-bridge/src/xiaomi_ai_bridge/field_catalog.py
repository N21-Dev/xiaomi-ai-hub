"""Ecosystem-neutral, bounded metadata for every visible state field."""

from __future__ import annotations

import copy
import math
import re
from datetime import UTC, datetime
from typing import Any

from .capability_catalog import (
    DEFAULT_UNITS,
    NUMERIC_FORMATS,
    PROPERTY_LABELS,
    UNIT_LABELS,
)
from .data_policy import policy_for_field
from .field_presentation import presentation_for_field, semantic_role_for_field
from .logging_setup import scrub

FIELD_TYPES = {
    "number",
    "string",
    "boolean",
    "enum",
    "array",
    "object",
    "null",
    "unknown",
}
FIELD_STATUSES = {
    "ok",
    "unavailable",
    "unsupported",
    "offline",
    "not_readable",
    "sensitive",
    "stale",
    "unknown",
}
FIELD_ROLES = {
    "measurement",
    "counter",
    "state",
    "setting",
    "identity",
    "metadata",
    "collection",
    "event",
    "parameter",
}
_IDENTITY_FIELD = re.compile(
    r"serial|uuid|guid|imei|imsi|(^|[-_. /])(id|did|uid|mac|bssid|ip)([-_. /]|$)|"
    r"序列号|标识|编号|账号|账户",
    re.IGNORECASE,
)
_METADATA_FIELD = re.compile(
    r"model|manufacturer|firmware|hardware.*version|revision|version|address|ssid|"
    r"(^|[-_. /])(url|name|description|timestamp|date)([-_. /]|$)|[-_]at$|"
    r"型号|固件|版本|厂商|名称|更新时间|采集时间",
    re.IGNORECASE,
)
_SETTINGS = {
    "volume",
    "brightness",
    "color-temperature",
    "target-temperature",
    "target-humidity",
    "fan-level",
    "loop",
}
STATUS_MESSAGES = {
    "ok": "已读取",
    "unavailable": "本次未取得数据",
    "unsupported": "当前接口不支持读取",
    "offline": "设备离线，无法读取",
    "not_readable": "设备未开放读取权限",
    "sensitive": "敏感字段已隐藏",
    "stale": "数据已过期，等待更新",
    "unknown": "尚未读取",
}
MAX_FIELDS = 2048
MAX_DEPTH = 8
REDACTED = "[已脱敏]"
_SOURCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,79}$")
_TOKEN_COUNTERS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "token_count",
    "tokens_used",
}
_SENSITIVE = re.compile(
    r"password|passwd|pass.?token|secret|token|cookie|api.?key|authorization|"
    r"private.?key|credential|security.?key|pairing.?code|pin.?code|auth.?code|jwt|"
    r"密码|口令|令牌|密钥|凭据|验证码",
    re.IGNORECASE,
)
_ALIASES = {
    "temperature-c": "temperature",
    "temperature-celsius": "temperature",
    "humidity-percent": "humidity",
    "humidity-pct": "humidity",
    "pm25": "pm2.5-density",
    "pm2.5": "pm2.5-density",
    "pm10": "pm10-density",
    "co2": "co2-density",
    "co2-ppm": "co2-density",
    "battery-percent": "battery-level",
    "battery": "battery-level",
    "progress-percent": "progress",
    "progress-pct": "progress",
}
_LABELS = {
    **PROPERTY_LABELS,
    "playback": "播放状态",
    "loop": "循环模式",
    "updated-at": "更新时间",
    "observed-at": "采集时间",
    "received-at": "接收时间",
    "attempted-at": "读取尝试时间",
    "progress": "进度",
    "active-threads": "进行中任务",
    "completed-threads": "已完成任务",
    "total-threads": "任务总数",
    "threads": "任务列表",
    "cpu-percent": "CPU 使用率",
    "memory-percent": "内存使用率",
    "disk-percent": "磁盘使用率",
    "uptime-seconds": "运行时长",
    "latency-ms": "响应耗时",
    "error": "错误信息",
    "error-count": "错误次数",
    "task": "任务",
    "summary": "摘要",
    "source": "来源",
    "environment": "环境",
    "sensors": "传感器",
    "values": "数值",
    "metrics": "指标",
    "devices": "设备列表",
    "enabled": "已启用",
    "message": "说明",
    "count": "数量",
    "input-tokens": "输入 Token 数",
    "output-tokens": "输出 Token 数",
    "total-tokens": "总 Token 数",
    "prompt-tokens": "提示 Token 数",
    "completion-tokens": "输出 Token 数",
    "cached-tokens": "缓存 Token 数",
    "reasoning-tokens": "推理 Token 数",
    "token-count": "Token 数",
    "tokens-used": "已用 Token 数",
}
_UNITS = {
    **DEFAULT_UNITS,
    "volume": "%",
    "brightness": "%",
    "progress": "%",
    "cpu-percent": "%",
    "memory-percent": "%",
    "disk-percent": "%",
    "uptime-seconds": "秒",
    "latency-ms": "毫秒",
    "rssi": "dBm",
    "electric-power": "W",
    "power": "W",
    "voltage": "V",
    "current": "A",
    "heart-rate": "次/分",
    "blood-oxygen": "%",
    "steps": "步",
    "step-count": "步",
}
_OPTIONS = {
    "playback": [
        {"value": "playing", "label": "播放中"},
        {"value": "paused", "label": "已暂停"},
        {"value": "idle", "label": "空闲"},
        {"value": "unknown", "label": "未知"},
    ],
    "loop": [
        {"value": 0, "label": "单曲循环"},
        {"value": 1, "label": "列表循环"},
    ],
}


def normalize_source(value: Any = None) -> str:
    if value is None or value == "":
        return "external"
    if (
        not isinstance(value, str)
        or not _SOURCE.fullmatch(value)
        or scrub(value) != value
    ):
        raise ValueError("source 必须是 1 到 80 个字符的来源标识")
    return value


def is_sensitive_path(path: list[str | int]) -> bool:
    return any(
        isinstance(part, str)
        and part.lower().replace("-", "_") not in _TOKEN_COUNTERS
        and bool(_SENSITIVE.search(part))
        for part in path
    )


def _text(value: Any, maximum: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"字段 {name} 必须是不超过 {maximum} 个字符的文本")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"字段 {name} 含有无效控制字符")
    return scrub(value).strip()


def _timestamp(value: Any, name: str) -> str:
    if value in (None, ""):
        return ""
    text = _text(value, 80, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"字段 {name} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _path(value: Any, key: str) -> list[str | int]:
    path = [key] if value is None else value
    if not isinstance(path, list) or not 1 <= len(path) <= MAX_DEPTH + 1:
        raise ValueError("字段 path 必须是长度 1 到 9 的路径数组")
    for part in path:
        if type(part) is int:
            if not 0 <= part < 256:
                raise ValueError("字段数组索引必须在 0 到 255 之间")
        elif not isinstance(part, str) or not part or _text(part, 128, "path") != part:
            raise ValueError("字段路径必须由非空名称或数组索引组成")
    return list(path)


def _options(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 256:
        raise ValueError("字段 options 必须是不超过 256 项的数组")
    output = []
    for item in raw:
        if not isinstance(item, dict) or "value" not in item:
            raise ValueError("字段选项必须包含 value")
        value = item["value"]
        if isinstance(value, str):
            value = _text(value, 200, "options.value")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("字段选项值必须为标量")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("字段选项数值必须有限")
        output.append(
            {
                "value": value,
                "label": _text(item.get("label", str(value)), 120, "options.label"),
            }
        )
    return output


def normalize_fields(raw: Any) -> list[dict[str, Any]]:
    """Discard unknown metadata keys and never accept caller-supplied values."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_FIELDS:
        raise ValueError(f"fields 必须是不超过 {MAX_FIELDS} 项的数组")
    output = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("每个字段声明必须是对象")
        key = _text(item.get("key"), 1200, "key")
        if not key:
            raise ValueError("字段 key 不能为空")
        path = _path(item.get("path"), key)
        identity = tuple(path)
        if identity in seen:
            raise ValueError("字段 path 不能重复")
        seen.add(identity)
        field: dict[str, Any] = {"key": key, "path": path}
        for name, maximum in (
            ("label", 160),
            ("unit", 40),
            ("semantic", 100),
            ("service", 120),
        ):
            if name in item:
                field[name] = _text(item[name], maximum, name)
        for name in ("readable", "writable", "sensitive", "notifiable"):
            if name in item:
                if type(item[name]) is not bool:
                    raise ValueError(f"字段 {name} 必须为布尔值")
                field[name] = item[name]
        if "type" in item:
            if not isinstance(item["type"], str) or item["type"] not in FIELD_TYPES:
                raise ValueError("字段 type 不受支持")
            field["type"] = item["type"]
        elif "format" in item:
            fmt = _text(item["format"], 32, "format")
            field["type"] = (
                "number"
                if fmt in NUMERIC_FORMATS
                else "boolean"
                if fmt in {"bool", "boolean"}
                else fmt
                if fmt in FIELD_TYPES
                else "unknown"
            )
        if "role" in item:
            if not isinstance(item["role"], str) or item["role"] not in FIELD_ROLES:
                raise ValueError("字段 role 不受支持")
            field["role"] = item["role"]
        if "usage" in item:
            if not isinstance(item["usage"], str) or item["usage"] not in {
                "action_input",
                "action_output",
                "event_argument",
            }:
                raise ValueError("字段 usage 不受支持")
            field["usage"] = item["usage"]
        for name in ("chartable", "comparable"):
            if name in item:
                if type(item[name]) is not bool:
                    raise TypeError(f"字段 {name} 必须为布尔值")
                field[name] = item[name]
        if "priority" in item:
            if not isinstance(item["priority"], str) or item["priority"] not in {
                "primary",
                "secondary",
                "detail",
            }:
                raise ValueError("字段 priority 不受支持")
            field["priority"] = item["priority"]
        for name in ("refresh_seconds", "cache_ttl_seconds"):
            if name in item:
                if type(item[name]) is not int or not 0 <= item[name] <= 259200:
                    raise ValueError(f"字段 {name} 必须是有效的秒数")
                field[name] = item[name]
        for name in ("layer", "refresh_policy"):
            if name in item:
                field[name] = _text(item[name], 100, name)
        if "source" in item:
            field["source"] = normalize_source(item["source"])
        if "options" in item:
            field["options"] = _options(item["options"])
            if field["options"] and field.get("type") != "boolean":
                field["type"] = "enum"
        diagnostic = item.get("read_status", {})
        if not isinstance(diagnostic, dict):
            raise TypeError("字段 read_status 必须是对象")
        for name in (
            "status",
            "message",
            "code",
            "source",
            "source_field",
            "source_updated_at",
            "source_age_seconds",
            "source_sample_cached",
            "observed_at",
            "attempted_at",
            "priority",
            "layer",
            "refresh_policy",
            "refresh_seconds",
            "cache_ttl_seconds",
            "cached",
            "next_refresh_at",
        ):
            if name in diagnostic:
                value = diagnostic[name]
            elif name in item:
                value = item[name]
            else:
                continue
            if name == "status":
                if not isinstance(value, str) or value not in FIELD_STATUSES:
                    raise ValueError("字段 status 不受支持")
                field[name] = value
            elif name == "code":
                if type(value) is int:
                    field[name] = value
                elif isinstance(value, str):
                    field[name] = _text(value, 80, name)
                elif value is not None:
                    raise ValueError("字段 code 必须是整数或短文本")
            elif name == "source":
                field[name] = normalize_source(value)
            elif name == "priority":
                if not isinstance(value, str) or value not in {
                    "primary",
                    "secondary",
                    "detail",
                }:
                    raise ValueError("字段 priority 不受支持")
                field[name] = value
            elif name in {"refresh_seconds", "cache_ttl_seconds"}:
                if type(value) is not int or not 0 <= value <= 259200:
                    raise ValueError(f"字段 {name} 必须是有效的秒数")
                field[name] = value
            elif name == "cached":
                if type(value) is not bool:
                    raise ValueError("字段 cached 必须为布尔值")
                field[name] = value
            elif name in {"observed_at", "attempted_at", "next_refresh_at", "source_updated_at"}:
                timestamp = _timestamp(value, name)
                if timestamp:
                    field[name] = timestamp
            elif name == "source_age_seconds":
                if type(value) is not int or value < 0:
                    raise ValueError("字段 source_age_seconds 必须是非负整数")
                field[name] = value
            elif name == "source_sample_cached":
                if type(value) is not bool:
                    raise ValueError("字段 source_sample_cached 必须为布尔值")
                field[name] = value
            else:
                field[name] = _text(value, 300, name)
        if "reason" in item and not field.get("message"):
            reason = _text(item["reason"], 300, "reason")
            if reason and not field.get("readable", True):
                field["message"] = reason
        if (
            is_sensitive_path(path)
            or field.get("sensitive")
            or field.get("status") == "sensitive"
        ):
            field.update(
                sensitive=True, status="sensitive", readable=False, writable=False
            )
            field["message"] = STATUS_MESSAGES["sensitive"]
            field.pop("options", None)
            field.pop("code", None)
        output.append(field)
    return output


def value_at(state: Any, path: list[str | int]) -> tuple[bool, Any]:
    value = state
    for part in path:
        if (isinstance(value, dict) and isinstance(part, str) and part in value) or (
            isinstance(value, list) and type(part) is int and 0 <= part < len(value)
        ):
            value = value[part]
        else:
            return False, None
    return True, value


def redact_sensitive_fields(
    state: dict[str, Any], fields: list[dict[str, Any]]
) -> dict[str, Any]:
    clean = copy.deepcopy(state)
    for field in fields:
        if not field.get("sensitive") and not is_sensitive_path(field["path"]):
            continue
        path = field["path"]
        found, parent = value_at(clean, path[:-1])
        if found:
            final = path[-1]
            if (
                isinstance(parent, dict) and isinstance(final, str) and final in parent
            ) or (
                isinstance(parent, list)
                and type(final) is int
                and 0 <= final < len(parent)
            ):
                parent[final] = REDACTED
    return clean


def sanitize_state_value(value: Any, *, depth: int = 0) -> Any:
    """Retain every bounded key; reject oversize structures instead of truncating them."""
    if depth > MAX_DEPTH:
        raise ValueError("状态嵌套层级超过 8 层限制")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("状态数值必须是有限数字")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = scrub(value)
        return text if len(text) <= 500 else text[:499] + "…"
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("单个状态数组超过 256 项限制")
        return [sanitize_state_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("单个状态对象超过 256 个字段限制")
        output = {}
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError("状态字段名必须是 1 到 128 个字符的文本")
            if scrub(key) != key or any(ord(char) < 32 for char in key):
                raise ValueError("状态字段名含有敏感信息或无效控制字符")
            invalid_counter = key.lower().replace("-", "_") in _TOKEN_COUNTERS and not (
                (type(item) is int or (type(item) is float and math.isfinite(item)))
                and item >= 0
            )
            output[key] = (
                REDACTED
                if is_sensitive_path([key]) or invalid_counter
                else sanitize_state_value(item, depth=depth + 1)
            )
        return output
    raise TypeError("状态值必须是 JSON 支持的类型")


def _field_key(path: list[str | int]) -> str:
    if len(path) == 1 and isinstance(path[0], str):
        return path[0]
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def _infer_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _semantic(path: list[str | int]) -> str:
    part = next(
        (str(item) for item in reversed(path) if isinstance(item, str)), "value"
    )
    # Only use dot suffixes when the full key has no known meaning (PM2.5 is literal).
    part = part.lower().replace("_", "-")
    if part not in _LABELS and part not in _ALIASES:
        part = part.rsplit(".", 1)[-1]
    return _ALIASES.get(part, part)


def _classify_field(field: dict[str, Any]) -> None:
    """Meaning determines analysis eligibility; numeric IDs stay descriptive."""
    semantic = field["semantic"]
    path = field["path"]
    # An ancestor called "device_id" or "firmware" describes its container,
    # not a nested temperature reading. Sensitivity still checks the full path.
    terminal = _semantic(path)
    source_terminal = re.split(r"[./]", field.get("source_field", ""))[-1]
    names = [terminal, semantic, field.get("label", ""), source_terminal]
    semantic_role = semantic_role_for_field(field)
    if field.get("role") in {"parameter", "event"} and not field.get("readable", True):
        role = field["role"]
    elif any(_IDENTITY_FIELD.search(name) for name in names):
        role = "identity"
    elif field.get("sensitive") or any(_METADATA_FIELD.search(name) for name in names):
        role = "metadata"
    elif field["type"] in {"array", "object"}:
        role = "collection"
    elif not field.get("readable", True) and field.get("notifiable"):
        role = "event"
    elif (
        field.get("writable")
        and field["type"] in {"boolean", "enum"}
        and (
            semantic not in {"on", "mode", "status", "playback", "fan-level"}
            or any(
                re.search(
                    r"indicator|child.?lock|wifi|wlan|network|microphone|no.?disturb|screen|display|指示灯|童锁|无线|勿扰|麦克风",
                    name,
                    re.IGNORECASE,
                )
                for name in names
            )
        )
    ):
        role = "setting"
    elif field["type"] in {"boolean", "enum"}:
        role = "state"
    elif field.get("writable") or semantic in _SETTINGS:
        role = "setting"
    elif semantic_role:
        role = semantic_role
    elif field.get("role"):
        role = field["role"]
    elif (
        semantic
        in {
            "steps",
            "step-count",
            "error-count",
            "active-threads",
            "completed-threads",
            "total-threads",
        }
        or semantic.replace("-", "_") in _TOKEN_COUNTERS
    ):
        role = "counter"
    elif field["type"] == "number" and (
        field.get("unit") or semantic in _UNITS or semantic in DEFAULT_UNITS
    ):
        role = "measurement"
    else:
        # A new ecosystem's unexplained number needs a meaning/unit declaration
        # before it can enter trend or comparison views.
        role = "metadata"
    eligible = (
        role in {"measurement", "counter"}
        and field["type"] == "number"
        and field.get("readable", True)
        and not field.get("sensitive")
    )
    field.update(
        role=role,
        chartable=eligible and field.get("chartable", True),
        comparable=eligible and field.get("comparable", True),
    )


def build_field_catalog(
    state: dict[str, Any],
    declarations: Any = None,
    *,
    source: str = "external",
    observed_at: str | None = None,
    attempted_at: str | None = None,
    stale: bool = False,
    offline: bool = False,
    previous: Any = None,
    freshness: bool = False,
) -> list[dict[str, Any]]:
    """Build fields, optionally checking each live sample's own expiration.

    Historical replay leaves ``freshness`` false so a sample's original read
    status does not depend on the time at which its history is viewed.
    """
    source = normalize_source(source)
    observed_at = _timestamp(observed_at, "observed_at")
    attempted_at = _timestamp(attempted_at, "attempted_at")
    merged: dict[tuple[str | int, ...], dict[str, Any]] = {}
    for field in normalize_fields(previous):
        present, value = value_at(state, field["path"])
        if present and value is not None and not field.get("sensitive"):
            for name in ("status", "message", "code", "observed_at", "attempted_at"):
                field.pop(name, None)
            field["source"] = source
            if field.get("type") in {"null", "unknown"}:
                field.pop("type", None)
        elif field.get("status") in {"ok", "stale", "offline"}:
            field["status"] = "unavailable"
            field["message"] = STATUS_MESSAGES["unavailable"]
            field.pop("code", None)
        merged[tuple(field["path"])] = field
    for field in normalize_fields(declarations):
        identity = tuple(field["path"])
        merged[identity] = {**merged.get(identity, {}), **field}
    clean_state = redact_sensitive_fields(
        sanitize_state_value(state), list(merged.values())
    )

    def visit(value: Any, path: list[str | int]) -> None:
        if path:
            merged.setdefault(tuple(path), {"key": _field_key(path), "path": path})
        if len(merged) > MAX_FIELDS:
            raise ValueError(f"完整字段数超过 {MAX_FIELDS} 项限制")
        if len(path) > MAX_DEPTH + 1:
            raise ValueError("字段嵌套层级过深")
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, [*path, key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, [*path, index])

    visit(clean_state, [])
    output = []
    offline = offline or clean_state.get("status") == "offline"
    for field in merged.values():
        path = field["path"]
        present, value = value_at(clean_state, path)
        semantic = (field.get("semantic") or _semantic(path))[:100]
        sensitive = (
            field.get("sensitive") or is_sensitive_path(path) or value == REDACTED
        )
        status = field.get(
            "status", "ok" if present and value is not None else "unavailable"
        )
        if sensitive:
            status = "sensitive"
        elif not field.get("readable", True):
            status = "not_readable"
        elif status == "ok" and (not present or value is None):
            status = "unavailable"
        if offline and status in {"ok", "unavailable", "unknown", "stale"}:
            status = "offline"
        prior_status = field.get("status")
        field.update(
            label=field.get("label") or _LABELS.get(semantic, str(path[-1])),
            unit=UNIT_LABELS.get(field.get("unit", ""), field.get("unit", ""))
            if "unit" in field
            else _UNITS.get(semantic, ""),
            type=field.get("type") or (_infer_type(value) if present else "unknown"),
            semantic=semantic,
            source=field.get("source") or source,
            status=status,
            readable=field.get("readable", True) and not sensitive,
            writable=field.get("writable", False) and not sensitive,
        )
        if status != prior_status or not field.get("message"):
            field["message"] = STATUS_MESSAGES[status]
        # A resource-wide change supersedes an old successful read message.
        if status in {"stale", "offline", "sensitive"}:
            field["message"] = STATUS_MESSAGES[status]
        field.setdefault("message", STATUS_MESSAGES[status])
        if sensitive:
            field["sensitive"] = True
            field.pop("options", None)
        elif "options" not in field and semantic in _OPTIONS:
            field["options"] = copy.deepcopy(_OPTIONS[semantic])
        if field.get("options") and field["type"] != "boolean":
            field["type"] = "enum"
        _classify_field(field)
        field.update(policy_for_field(field))
        if (stale or freshness) and status == "ok":
            sampled = field.get("observed_at") or observed_at
            ttl = field["cache_ttl_seconds"]
            # Unscheduled external fields have no independent TTL; their
            # resource expiration remains authoritative.
            if stale or ttl:
                valid_cache = False
                if sampled and ttl:
                    age = (
                        datetime.now(UTC) - datetime.fromisoformat(sampled)
                    ).total_seconds()
                    valid_cache = 0 <= age <= ttl
                if valid_cache:
                    if stale:
                        field.update(cached=True, message="使用仍在有效期内的缓存读数")
                else:
                    status = "stale"
                    field.update(status=status, message=STATUS_MESSAGES[status])
        if present and status in {"ok", "stale"}:
            field["value"] = copy.deepcopy(value)
            if observed_at and not field.get("observed_at"):
                field["observed_at"] = observed_at
        if attempted_at and not field.get("attempted_at"):
            field["attempted_at"] = attempted_at
        if isinstance(value, (dict, list)) and present:
            field["summary"] = f"{len(value)} 项"
        field["presentation"] = presentation_for_field(field)
        output.append(field)
    return output
