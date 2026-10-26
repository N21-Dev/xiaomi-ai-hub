"""Translate complete MIoT specifications into validated, presentable capabilities.

This module builds metadata only. It neither discovers devices nor reads or writes
their state. Capability presence must never be confused with permission to execute.
"""

from __future__ import annotations

import math
import re
from typing import Any

CATEGORY_LABELS = {
    "light": "灯",
    "outlet": "插座",
    "switch": "开关",
    "air-conditioner": "空调",
    "air-purifier": "空气净化器",
    "fan": "风扇",
    "speaker": "音箱",
    "television": "电视",
    "tv-box": "电视盒子",
    "router": "路由器",
    "watch": "手表",
    "smartwatch": "智能手表",
    "band": "手环",
    "ir-remote-control": "红外遥控器",
    "remote-control": "遥控器",
    "humidifier": "加湿器",
    "dehumidifier": "除湿机",
    "air-fresh": "新风机",
    "air-monitor": "空气检测仪",
    "sensor": "传感器",
    "vacuum": "扫地机器人",
    "curtain": "窗帘",
    "heater": "取暖器",
    "water-heater": "热水器",
    "water-purifier": "净水器",
    "washing-machine": "洗衣机",
    "refrigerator": "冰箱",
    "camera": "摄像头",
    "lock": "门锁",
    "door": "门",
    "gateway": "网关",
    "body-scale": "体脂秤",
}

SERVICE_LABELS = {
    **CATEGORY_LABELS,
    "device-information": "设备信息",
    "environment": "环境",
    "battery": "电池",
    "indicator-light": "指示灯",
    "night-light": "夜灯",
    "screen": "屏幕",
    "display": "显示屏",
    "physical-controls-locked": "童锁",
    "filter": "滤芯",
    "filter-1": "滤芯一",
    "filter-2": "滤芯二",
    "alarm": "提醒",
    "audio": "音频",
    "play-control": "播放控制",
    "media-player": "媒体播放",
    "microphone": "麦克风",
    "network": "网络",
    "wifi": "无线网络",
    "wifi-network": "无线网络",
    "wireless-network": "无线网络",
    "wifi-router": "路由网络",
    "mesh": "Mesh 网络",
    "bluetooth": "蓝牙",
    "clock": "时钟",
    "timer": "定时",
    "message-router": "消息路由",
    "intelligent-speaker": "智能音箱",
    "speaker-playback": "音箱播放",
    "input-control": "输入源",
    "volume": "音量",
    "television-control": "电视控制",
    "remote": "遥控",
    "ir": "红外",
    "ir-code": "红外码",
    "heartbeat": "心率",
    "heart-rate": "心率",
    "blood-oxygen": "血氧",
    "fitness": "运动",
    "sleep": "睡眠",
    "step-counter": "步数",
    "motion": "运动检测",
    "power-consumption": "用电量",
    "power-saving": "节能",
    "custom": "扩展功能",
    "yeelight": "灯光扩展",
    "air-purifier-favorite": "净化器最爱模式",
    "vital-signs": "身体指标",
    "guest-wifi": "访客网络",
    "ble-control": "蓝牙控制",
    "motion-data": "运动数据",
    "device-status": "设备状态",
    "vibration": "振动",
    "wear-car-interconnect": "穿戴与车辆互联",
}

PROPERTY_LABELS = {
    "on": "电源",
    "brightness": "亮度",
    "color-temperature": "色温",
    "color": "颜色",
    "rgb": "RGB 颜色",
    "hue": "色相",
    "saturation": "饱和度",
    "mode": "运行模式",
    "fan-level": "风速档位",
    "fan-speed": "风速",
    "target-temperature": "目标温度",
    "temperature": "温度",
    "relative-humidity": "湿度",
    "humidity": "湿度",
    "target-humidity": "目标湿度",
    "pm2.5-density": "PM2.5",
    "pm10-density": "PM10",
    "co2-density": "二氧化碳",
    "air-quality": "空气质量",
    "air-quality-index": "空气质量指数",
    "atmospheric-pressure": "气压",
    "tvoc-density": "TVOC",
    "voc-density": "VOC",
    "formaldehyde-density": "甲醛浓度",
    "filter-life-level": "滤芯剩余寿命",
    "filter-left-time": "滤芯剩余时间",
    "filter-used-time": "滤芯使用时间",
    "filter-used-flow": "滤芯累计用量",
    "filter-left-flow": "滤芯剩余用量",
    "filter-life": "滤芯寿命",
    "filter-hours-used": "滤芯使用时长",
    "favorite-level": "最爱档位",
    "favorite-speed": "最爱风速",
    "favorite-fan-level": "最爱风速",
    "motor-speed": "电机转速",
    "horizontal-swing": "左右摆风",
    "vertical-swing": "上下摆风",
    "swing": "摆风",
    "swing-angle": "摆风角度",
    "fault": "故障状态",
    "status": "状态",
    "working-time": "运行时间",
    "use-time": "使用时间",
    "power-consumption": "用电量",
    "electric-power": "功率",
    "voltage": "电压",
    "electric-current": "电流",
    "current": "电流",
    "power": "功率",
    "battery-level": "电量",
    "charging-state": "充电状态",
    "battery-power": "电池电量",
    "volume": "音量",
    "mute": "静音",
    "playback-state": "播放状态",
    "play-state": "播放状态",
    "playing-state": "播放状态",
    "title": "标题",
    "artist": "歌手",
    "album": "专辑",
    "position": "播放位置",
    "duration": "时长",
    "input-source": "输入源",
    "channel": "频道",
    "channel-id": "频道",
    "resolution": "分辨率",
    "picture-mode": "画面模式",
    "sound-mode": "音效模式",
    "screen-brightness": "屏幕亮度",
    "text-content": "播报文本",
    "text": "文本",
    "url": "媒体地址",
    "playing-url": "播放地址",
    "wake-up": "唤醒",
    "do-not-disturb": "免打扰",
    "silent-mode": "静音模式",
    "night-mode": "夜间模式",
    "sleep-mode": "睡眠模式",
    "alarm": "声音提醒",
    "alarm-volume": "提醒音量",
    "child-lock": "童锁",
    "physical-controls-locked": "按键锁定",
    "countdown-time": "倒计时",
    "off-delay-time": "延时关闭",
    "on-delay-time": "延时开启",
    "timer": "定时",
    "manufacturer": "制造商",
    "model": "型号",
    "serial-number": "序列号",
    "firmware-revision": "固件版本",
    "hardware-revision": "硬件版本",
    "software-revision": "软件版本",
    "device-name": "设备名称",
    "name": "名称",
    "ssid": "无线网络名称",
    "wifi-ssid": "无线网络名称",
    "rssi": "信号强度",
    "signal-strength": "信号强度",
    "ip-address": "IP 地址",
    "mac-address": "MAC 地址",
    "connection-status": "连接状态",
    "connected": "已连接",
    "online": "在线",
    "download-speed": "下载速率",
    "upload-speed": "上传速率",
    "connected-device-count": "连接设备数",
    "bandwidth": "带宽",
    "password": "密码",
    "wifi-password": "无线网络密码",
    "token": "访问令牌",
    "security": "安全凭据",
    "security-key": "安全密钥",
    "access-token": "访问令牌",
    "heart-rate": "心率",
    "blood-oxygen": "血氧",
    "steps": "步数",
    "step-count": "步数",
    "calories": "热量消耗",
    "distance": "距离",
    "weight": "体重",
    "body-fat-rate": "体脂率",
    "sleep-duration": "睡眠时长",
    "ir-code": "红外码",
    "key-code": "按键编码",
    "remote-id": "遥控器编号",
    "learn-status": "学习状态",
    "target-position": "目标位置",
    "current-position": "当前位置",
    "water-level": "水位",
    "tds": "TDS 水质",
    "connected-device-number": "已连接设备数",
    "network-connection-type": "上网方式",
    "ppp-username": "宽带账号",
    "static-ip-address": "静态 IP 地址",
    "subnet-mask": "子网掩码",
    "gateway-ip-address": "网关地址",
    "dns-ip-address": "DNS 地址",
    "dns-auto-mode": "自动获取 DNS",
    "access-mode": "接入方式",
    "wifi-band-combine": "双频合一",
    "online-time": "在线时长",
    "wifi-ssid-hidden": "隐藏无线网络名称",
    "encrypted-strength": "加密方式",
    "wifi-channel": "无线信道",
    "wifi-bandwidth": "无线频宽",
    "speed-control": "访客限速",
    "max-upload-speed": "上传限速",
    "max-download-speed": "下载限速",
    "input-control": "输入源",
    "state-playing": "播放状态",
    "default-power-on-state": "通电后自动亮灯",
    "silent-execution": "静默执行",
}
# Kept public for consumers that need the same names outside of a full catalog.
property_labels = PROPERTY_LABELS

ACTION_LABELS = {
    "play": "播放",
    "pause": "暂停",
    "stop": "停止",
    "next": "下一首",
    "previous": "上一首",
    "play-music": "播放音乐",
    "play-text": "播报文字",
    "wake-up": "唤醒",
    "volume-up": "增大音量",
    "volume-down": "减小音量",
    "reboot": "重启",
    "restart": "重启",
    "factory-reset": "恢复出厂设置",
    "reset-filter-life": "重置滤芯寿命",
    "start": "开始",
    "cancel": "取消",
    "learn": "学习遥控",
    "send-code": "发送遥控码",
    "send-key": "发送按键",
    "execute-text-directive": "执行文本指令",
    "post": "发送消息",
    "start-measure": "开始测量",
    "start-workout": "开始运动",
    "turn-on": "开机",
    "turn-off": "关机",
    "toggle": "切换开关",
    "press-home": "主页",
    "press-menu": "菜单",
    "press-settings": "设置",
    "press-back": "返回",
    "press-left": "向左",
    "press-right": "向右",
    "press-up": "向上",
    "press-down": "向下",
    "press-ok": "确定",
    "press-volume-down": "音量减小",
    "press-volume-up": "音量增大",
    "press-play": "播放",
    "press-pause": "暂停",
    "press-play-pause": "播放／暂停",
    "play-radio": "播放电台",
    "stop-alarm": "停止闹钟",
}

OPTION_LABELS = {
    "auto": "自动",
    "automatic": "自动",
    "sleep": "睡眠",
    "favorite": "最爱",
    "free": "自由调节",
    "computer": "电脑",
    "warmth": "温馨",
    "leisure": "休闲",
    "office": "办公",
    "reading": "阅读",
    "entertainment": "娱乐",
    "cool": "制冷",
    "heat": "制热",
    "dry": "除湿",
    "fan": "送风",
    "low": "低",
    "medium": "中",
    "high": "高",
    "silent": "静音",
    "on": "开启",
    "off": "关闭",
    "open": "开启",
    "close": "关闭",
    "idle": "待机",
    "playing": "播放中",
    "paused": "已暂停",
    "stopped": "已停止",
    "normal": "正常",
    "none": "无",
    "fault": "故障",
    "charging": "充电中",
    "full": "已充满",
    "not-charging": "未充电",
    "enabled": "启用",
    "disabled": "停用",
    "standard": "标准",
    "eco": "节能",
    "strong": "强力",
    "soft": "柔和",
    "good": "良好",
    "poor": "较差",
}

UNIT_LABELS = {
    "celsius": "℃",
    "fahrenheit": "℉",
    "percentage": "%",
    "percent": "%",
    "kelvin": "K",
    "microgram-per-cubic-meter": "μg/m³",
    "ppm": "ppm",
    "pascal": "Pa",
    "hectopascal": "hPa",
    "rpm": "转/分",
    "seconds": "秒",
    "minutes": "分钟",
    "hours": "小时",
    "days": "天",
    "second": "秒",
    "minute": "分钟",
    "hour": "小时",
    "day": "天",
    "watt": "W",
    "volt": "V",
    "ampere": "A",
    "kilowatt-hour": "kWh",
    "lux": "lx",
    "degree": "°",
    "meter": "m",
    "kilometer": "km",
    "kilogram": "kg",
    "bpm": "次/分",
    "decibel": "dB",
    "none": "",
}

DEFAULT_UNITS = {
    "temperature": "℃",
    "target-temperature": "℃",
    "relative-humidity": "%",
    "humidity": "%",
    "target-humidity": "%",
    "color-temperature": "K",
    "pm2.5-density": "μg/m³",
    "pm10-density": "μg/m³",
    "co2-density": "ppm",
    "filter-life-level": "%",
    "battery-level": "%",
}

NUMERIC_FORMATS = {
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "int8",
    "int16",
    "int32",
    "int64",
    "int",
    "uint",
    "float",
    "double",
}
MAIN_SERVICES = {
    "light": {"light"},
    "outlet": {"switch", "outlet"},
    "air-purifier": {"air-purifier"},
    "speaker": {"speaker", "intelligent-speaker"},
    "television": {"television"},
    "tv-box": {"tv-box", "television"},
    "router": {"router", "wifi-router"},
}
ANCILLARY_POWER = {"indicator-light", "night-light", "screen", "display", "alarm"}
SENSITIVE = re.compile(
    r"password|passwd|pass-word|passcode|(^|-)pwd($|-)|token|secret|credential|"
    r"security|private-key|access-key|api-key|pin-code|pairing-code|auth-code|"
    r"密码|口令|令牌|密钥|凭据|验证码",
    re.IGNORECASE,
)
OPEN_COMMAND = re.compile(
    r"execute|directive|command|script|shell|factory-reset|format-disk|erase|"
    r"delete-all|clear-all|reset-all|flash-firmware|unlock|open-door|"
    r"恢复出厂|格式化|清除全部|执行指令",
    re.IGNORECASE,
)
HIGH_RISK = {"lock", "door", "garage-door", "gas-valve", "gas-stove", "cooker"}
HEALTH_CATEGORIES = {
    "watch",
    "smartwatch",
    "band",
    "body-scale",
    "treadmill",
    "blood-pressure-monitor",
}


def category_label(category: str) -> str:
    """Translate known categories while retaining unknown device types."""
    return CATEGORY_LABELS.get(category, category or "其他设备")


def _name(raw: dict[str, Any], kind: str, fallback: str) -> str:
    marker = f":{kind}:"
    urn = str(raw.get("type") or "")
    return urn.split(marker, 1)[1].split(":", 1)[0] if marker in urn else fallback


def _label(raw: dict[str, Any], name: str, dictionary: dict[str, str]) -> str:
    comment = str(raw.get("comment") or "").strip()
    if re.search(r"[\u4e00-\u9fff]", comment) and len(comment) <= 100:
        return comment
    return dictionary.get(name, str(raw.get("description") or name).strip())


def _iid(raw: Any) -> int | None:
    try:
        result = int(raw)
        return result if result > 0 and not isinstance(raw, bool) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _range(raw: Any) -> list[int | float]:
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    values = [*raw[:2], raw[2] if len(raw) > 2 else 1]
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in values
    ):
        return []
    return values if values[0] <= values[1] and values[2] > 0 else []


def _enum(raw: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    aliases: dict[str, Any] = {}
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or "value" not in item:
            continue
        value = item["value"]
        if not isinstance(value, (str, int, float, bool)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            continue
        identity = f"{type(value).__name__}:{value}"
        canonical = str(item.get("description") or value).strip()
        label = _label(item, canonical.casefold(), OPTION_LABELS)
        # Preserve custom English enum descriptions, including their case.
        if not label or label == canonical.casefold():
            label = canonical
        if canonical in aliases and aliases[canonical] != value:
            canonical = f"{canonical} ({value})"
        aliases[canonical] = value
        # Equal numeric raw values represent the same MIoT choice.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            identity = f"number:{float(value)}"
        for alias in (label, str(item.get("description") or "").strip()):
            if alias and (alias not in aliases or aliases[alias] == value):
                aliases[alias] = value
        if identity not in seen:
            options.append({"value": canonical, "label": label})
            seen.add(identity)
    return aliases, options


def _requires_confirmation(service: str, name: str) -> bool:
    return any(
        part in service
        for part in ("network", "wifi", "router", "heater", "gas", "cooker")
    ) or (service == "microphone" and name in {"mute", "on"})


def enum_options(value_list: dict[str, Any]) -> list[dict[str, str]]:
    """Render stored aliases once per raw value, preserving canonical inputs."""
    grouped: dict[str, dict[str, Any]] = {}
    for alias, value in value_list.items():
        identity = f"{type(value).__name__}:{value}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            identity = f"number:{float(value)}"
        entry = grouped.setdefault(identity, {"value": alias, "aliases": []})
        entry["aliases"].append(alias)
    result = []
    used_labels: set[str] = set()
    for entry in grouped.values():
        canonical = str(entry["value"])
        chinese = next(
            (
                alias
                for alias in entry["aliases"]
                if re.search(r"[\u4e00-\u9fff]", alias)
            ),
            "",
        )
        label = chinese or OPTION_LABELS.get(canonical.casefold(), canonical)
        if label in used_labels:
            label = f"{label}（{canonical}）"
        used_labels.add(label)
        result.append({"value": canonical, "label": label})
    return result


def _input_shape(prop: dict[str, Any], options: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {"format": prop["format"], "unit": prop["unit"]}
    if prop["format"] == "bool":
        result.update(kind="toggle", on_value=True, off_value=False)
    elif options:
        result.update(kind="select", options=options)
    elif prop["format"] in NUMERIC_FORMATS and prop["value_range"]:
        minimum, maximum, step = prop["value_range"]
        result.update(kind="range", min=minimum, max=maximum, step=step)
    elif prop["format"] == "string":
        result.update(kind="text", max_length=256)
    else:
        result.update(kind="text", unsupported=True)
    return result


def build_catalog(spec: dict[str, Any], category: str) -> dict[str, Any]:
    """Return all MIoT metadata and constrained control/action descriptions.

    ``properties`` contains backend protocol IDs; ``controls`` contains only
    opaque operation IDs and semantic values. Sensitive properties remain in the
    catalog for an honest capability display but can never be read or written.
    """
    properties: dict[str, dict[str, Any]] = {}
    services: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    actions: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    raw_services: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    property_ids: dict[tuple[int, int], str] = {}
    shapes: dict[str, dict[str, Any]] = {}
    options_by_key: dict[str, list[dict[str, str]]] = {}
    for raw in spec.get("services") or []:
        if not isinstance(raw, dict) or not (siid := _iid(raw.get("iid"))):
            continue
        name = _name(raw, "service", f"service-{siid}")
        service = {
            "key": f"s{siid}",
            "label": _label(raw, name, SERVICE_LABELS),
            "properties": [],
            "actions": [],
            "events": [],
        }
        services.append(service)
        raw_services.append((siid, name, raw, service))

    primary = MAIN_SERVICES.get(category, {category})
    # Main-service properties win the legacy bare keys regardless of spec order.
    ordered = sorted(
        raw_services,
        key=lambda item: (
            0 if item[1] in primary else 1 if item[1] == "environment" else 2
        ),
    )
    for siid, service_name, raw_service, service in ordered:
        for raw in raw_service.get("properties") or []:
            if not isinstance(raw, dict) or not (piid := _iid(raw.get("iid"))):
                continue
            name = _name(raw, "property", f"property-{piid}")
            state_key = name
            if state_key in properties or (
                name == "on" and service_name in ANCILLARY_POWER
            ):
                state_key = f"{service_name}.{name}"
            if state_key in properties:
                state_key = f"{service_name}.s{siid}.{name}"
            if state_key in properties:
                state_key += f".p{piid}"
            access = raw.get("access") or []
            sensitive = bool(
                SENSITIVE.search(
                    f"{service_name} {name} {raw.get('description', '')} {raw.get('comment', '')}"
                )
            )
            writable = "write" in access
            reason = "敏感凭据不读取、不写入" if sensitive else ""
            if category in HIGH_RISK or service_name in HIGH_RISK:
                reason = reason or "高风险设备控制尚未开放"
            aliases, options = _enum(raw.get("value-list"))
            unit = str(raw.get("unit") or "")
            prop = {
                "siid": siid,
                "piid": piid,
                "name": state_key,
                "format": str(raw.get("format") or "unknown"),
                "readable": "read" in access and not sensitive,
                "writable": writable and not sensitive and not reason,
                "value_range": _range(raw.get("value-range")),
                "value_list": {} if sensitive else aliases,
                "label": _label(raw, name, PROPERTY_LABELS),
                "service": service_name,
                "service_label": service["label"],
                "unit": UNIT_LABELS.get(unit, unit)
                if unit
                else DEFAULT_UNITS.get(name, ""),
                "notifiable": "notify" in access and not sensitive,
                "sensitive": sensitive,
                "control_id": f"prop_s{siid}_p{piid}" if writable else "",
                "blocked_reason": reason,
                "spec_name": name,
            }
            properties[state_key] = prop
            property_ids[siid, piid] = state_key
            service["properties"].append(state_key)
            options_by_key[state_key] = [] if sensitive else options
            shape = _input_shape(prop, options_by_key[state_key])
            shapes[state_key] = shape
            if writable:
                if shape.get("unsupported") and not reason:
                    reason = (
                        "设备未提供有效数值范围"
                        if prop["format"] in NUMERIC_FORMATS
                        else "暂不支持此参数格式"
                    )
                control = {
                    "action": prop["control_id"],
                    "property": state_key,
                    "label": prop["label"],
                    "service": service["label"],
                    **{
                        key: value
                        for key, value in shape.items()
                        if key != "unsupported"
                    },
                    "requires_confirmation": _requires_confirmation(service_name, name),
                    "enabled": not reason,
                    "reason": reason,
                }
                if shape["kind"] == "toggle":
                    control["off_action"] = prop["control_id"]
                if not prop["readable"]:
                    control["confirmation_note"] = "仅能确认设备已接收，无法回读验证"
                controls.append(control)
                prop["blocked_reason"] = reason

    def argument(siid: int, reference: Any) -> tuple[dict[str, Any], str]:
        piid = _iid(
            reference.get("piid", reference.get("iid"))
            if isinstance(reference, dict)
            else reference
        )
        key = property_ids.get((siid, piid)) if piid else None
        if key is None:
            return {
                "key": f"p{piid or '?'}",
                "property": "",
                "label": "未定义参数",
                "format": "unknown",
                "kind": "text",
            }, "动作参数缺少属性定义"
        prop = properties[key]
        shape = shapes[key]
        reason = prop["blocked_reason"] if prop["sensitive"] else ""
        if shape.get("unsupported"):
            reason = reason or "动作参数缺少有效范围或格式定义"
        return {
            "key": f"p{piid}",
            "property": key,
            "label": prop["label"],
            **{k: v for k, v in shape.items() if k != "unsupported"},
            "sensitive": prop["sensitive"],
        }, reason

    for siid, service_name, raw_service, service in raw_services:
        for raw in raw_service.get("actions") or []:
            if not isinstance(raw, dict) or not (aiid := _iid(raw.get("iid"))):
                continue
            name = _name(raw, "action", f"action-{aiid}")
            label = _label(raw, name, ACTION_LABELS)
            action_id = f"action_s{siid}_a{aiid}"
            inputs, outputs = [], []
            reason = ""
            for reference in raw.get("in") or []:
                value, arg_reason = argument(siid, reference)
                inputs.append(value)
                reason = reason or arg_reason
            for reference in raw.get("out") or []:
                value, _ = argument(siid, reference)
                outputs.append(value)
                if value.get("sensitive"):
                    reason = reason or "动作输出含敏感凭据，不开放执行"
            if (
                OPEN_COMMAND.search(f"{name} {label}")
                or service_name == "message-router"
                or name == "post"
            ):
                reason = "开放指令或破坏性动作不开放执行"
            if category in HEALTH_CATEGORIES or any(
                word in service_name
                for word in (
                    "heart-rate",
                    "blood-",
                    "fitness",
                    "workout",
                    "measurement",
                    "sport",
                )
            ):
                reason = "健康测量和运动动作请使用设备官方应用"
            if category in HIGH_RISK or service_name in HIGH_RISK:
                reason = "高风险设备动作尚未开放"
            actions[action_id] = {
                "action": action_id,
                "siid": siid,
                "aiid": aiid,
                "label": label,
                "service": service["label"],
                "inputs": inputs,
                "outputs": outputs,
                "enabled": not reason,
                "reason": reason,
                "requires_confirmation": True,
            }
            service["actions"].append(action_id)
        for raw in raw_service.get("events") or []:
            if not isinstance(raw, dict) or not (eiid := _iid(raw.get("iid"))):
                continue
            name = _name(raw, "event", f"event-{eiid}")
            event_id = f"event_s{siid}_e{eiid}"
            args = [argument(siid, ref)[0] for ref in raw.get("arguments") or []]
            events.append(
                {
                    "key": event_id,
                    "label": _label(raw, name, PROPERTY_LABELS),
                    "service": service["label"],
                    "arguments": args,
                    "status": "事件订阅未接入",
                }
            )
            service["events"].append(event_id)
    return {
        "properties": properties,
        "services": services,
        "controls": controls,
        "actions": actions,
        "events": events,
    }
