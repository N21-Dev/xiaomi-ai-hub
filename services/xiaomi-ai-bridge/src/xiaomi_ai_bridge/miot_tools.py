from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import stat
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from miservice import MiIOService, MiTokenStore

from .capability_catalog import build_catalog, category_label, enum_options
from .config import Settings
from .data_policy import POLICY_KEYS, policy_for_field
from .field_catalog import build_field_catalog
from .logging_setup import scrub
from .state import RuntimeConfigStore
from .xiaomi import PassTokenAccount

LOGGER = logging.getLogger(__name__)

SPEC_INSTANCES_URL = "https://miot-spec.org/miot-spec-v2/instances?status=all"
SPEC_INSTANCE_URL = "https://miot-spec.org/miot-spec-v2/instance"
TOOL_NAME = "control_xiaomi_home"
MAX_TOOL_CALLS = 3

SAFE_CATEGORIES = {
    "light": "灯",
    "outlet": "插座",
    "air-conditioner": "空调",
    "fan": "风扇",
    "air-purifier": "空气净化器",
}

MAIN_SERVICES = {
    "light": {"light"},
    "outlet": {"switch", "outlet"},
    "air-conditioner": {"air-conditioner"},
    "fan": {"fan"},
    "air-purifier": {"air-purifier", "environment"},
}

PROPERTY_ACTIONS = {
    "on": ("turn_on", "turn_off"),
    "brightness": ("set_brightness",),
    "color-temperature": ("set_color_temperature",),
    "target-temperature": ("set_temperature",),
    "mode": ("set_mode",),
    "fan-level": ("set_speed",),
}

READONLY_PROPERTIES = {
    "fault",
    "status",
    "air-quality",
    "air-quality-index",
    "atmospheric-pressure",
    "co2-density",
    "pm10-density",
    "pm2.5-density",
    "relative-humidity",
    "temperature",
    "filter-life-level",
    "filter-left-time",
    "filter-used-time",
    "filter-left-flow",
    "filter-used-flow",
}

VALUE_ALIASES = {
    "自动": "auto",
    "制冷": "cool",
    "除湿": "dry",
    "制热": "heat",
    "送风": "fan",
    "静音": "silent",
    "低": "low",
    "中": "medium",
    "高": "high",
    "睡眠": "sleep",
    "最爱": "favorite",
}

CONTROL_LABELS = {
    "on": ("电源", ""),
    "brightness": ("亮度", "%"),
    "color-temperature": ("色温", "K"),
    "target-temperature": ("目标温度", "℃"),
    "mode": ("运行模式", ""),
    "fan-level": ("风速档位", ""),
}
OPTION_LABELS = {
    "auto": "自动",
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
}


def _urn_name(value: object, kind: str) -> str:
    text = str(value or "")
    marker = f":{kind}:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split(":", 1)[0]


def _secure_replace(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


class MappedMiIOService(MiIOService):
    """Match cloud replies by property identity rather than response order."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.last_property_reads: dict[tuple[str, int, int], dict[str, Any]] = {}

    async def miot_get_props(self, did: str, iids: list[tuple[int, int]]) -> list[Any]:
        params = [{"did": did, "siid": s, "piid": p} for s, p in iids]
        attempted = datetime.now(UTC).isoformat()
        for siid, piid in iids:
            self.last_property_reads[(str(did), siid, piid)] = {
                "attempted_at": attempted,
                "source": "miot.cloud",
                "status": "unavailable",
                "message": "云端没有返回该属性",
            }
        result = await self.miot_request("prop/get", params)
        if not isinstance(result, list):
            raise TypeError("米家属性接口返回格式无效")
        values: dict[tuple[int, int], Any] = {}
        allowed = set(iids)
        for item in result:
            if not isinstance(item, dict) or str(item.get("did", did)) != str(did):
                continue
            identity = (item.get("siid"), item.get("piid"))
            if identity not in allowed:
                continue
            diagnostic = self.last_property_reads[(str(did), *identity)]
            code = item.get("code")
            if type(code) is int:
                diagnostic["code"] = code
            if type(code) is int and code == 0 and item.get("value") is not None:
                values[identity] = item["value"]
                diagnostic.update(
                    status="ok", message="读取成功", observed_at=attempted
                )
                source_updated_at = self._source_updated_at(item.get("updateTime"))
                if source_updated_at:
                    diagnostic["source_updated_at"] = source_updated_at
            elif type(code) is int and code in {-704002000, -704090001, -704220043}:
                diagnostic.update(
                    status="unsupported", message="设备或云端不支持此属性的直接读取"
                )
            elif type(code) is int and code != 0:
                diagnostic["message"] = "设备或云端拒绝了此属性读取"
        return [values.get(tuple(iid)) for iid in iids]

    @staticmethod
    def _source_updated_at(value: object) -> str:
        try:
            seconds = float(value)
            if seconds > 10**12:
                seconds /= 1000
            now = time.time()
            if not 946684800 <= seconds <= now + 300:
                return ""
            return datetime.fromtimestamp(seconds, UTC).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return ""


@dataclass
class PropertySpec:
    siid: int
    piid: int
    name: str
    format: str
    readable: bool
    writable: bool
    value_range: list[float] = field(default_factory=list)
    value_list: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    service: str = ""
    service_label: str = ""
    unit: str = ""
    notifiable: bool = False
    sensitive: bool = False
    control_id: str = ""
    blocked_reason: str = ""
    spec_name: str = ""


@dataclass
class DeviceRecord:
    key: str
    did: str
    name: str
    model: str
    category: str
    alias: str
    enabled: bool = False
    properties: dict[str, PropertySpec] = field(default_factory=dict)
    services: list[dict[str, Any]] = field(default_factory=list)
    catalog_controls: list[dict[str, Any]] = field(default_factory=list)
    catalog_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    catalog_events: list[dict[str, Any]] = field(default_factory=list)
    cloud_online: bool | None = None
    support_note: str = ""

    def actions(self) -> list[str]:
        actions = (
            ["get_state"] if any(p.readable for p in self.properties.values()) else []
        )
        for name, prop in self.properties.items():
            if (
                prop.writable
                and self.category in SAFE_CATEGORIES
                and not prop.blocked_reason
            ):
                actions.extend(PROPERTY_ACTIONS.get(name, ()))
        return actions

    def public_dict(self) -> dict[str, Any]:
        values: dict[str, list[str]] = {}
        for name, prop in self.properties.items():
            if prop.value_list:
                values[name] = list(prop.value_list)
            elif prop.value_range:
                values[name] = [str(item) for item in prop.value_range]
        return {
            "key": self.key,
            "name": self.name,
            "model": self.model,
            "category": self.category,
            "category_label": category_label(self.category),
            "alias": self.alias,
            "enabled": self.enabled,
            "actions": self.actions(),
            "values": values,
            "controls": self.control_definitions(),
            "extra_controls": [
                control
                for control in self.catalog_controls
                if control.get("property")
                not in {c["property"] for c in self.control_definitions()}
            ],
            "properties": [
                {
                    "key": key,
                    "label": p.label or p.spec_name or p.name,
                    "service": p.service_label or p.service,
                    "unit": p.unit,
                    "readable": p.readable,
                    "writable": p.writable,
                    "notifiable": p.notifiable,
                    "sensitive": p.sensitive,
                    "reason": p.blocked_reason,
                    "format": p.format,
                    "options": enum_options(p.value_list),
                }
                for key, p in self.properties.items()
            ],
            "services": self.services,
            "capabilities_actions": [
                {
                    **{k: v for k, v in action.items() if k not in {"siid", "aiid"}},
                    "action": action_id,
                }
                for action_id, action in self.catalog_actions.items()
            ],
            "capabilities_events": self.catalog_events,
            "cloud_online": self.cloud_online,
            "support_note": self.support_note,
        }

    def control_definitions(self) -> list[dict[str, Any]]:
        controls = []
        if self.category not in SAFE_CATEGORIES:
            return controls
        for name, prop in self.properties.items():
            if (
                name not in PROPERTY_ACTIONS
                or not prop.writable
                or not prop.readable
                or prop.blocked_reason
            ):
                continue
            label, unit = CONTROL_LABELS[name]
            control = {
                "action": PROPERTY_ACTIONS[name][0],
                "property": name,
                "label": label,
                "unit": unit,
            }
            if name == "on":
                if prop.format != "bool":
                    continue
                controls.append({**control, "kind": "toggle", "off_action": "turn_off"})
            elif prop.value_list:
                # Some purifiers expose their operating modes again as fan-level.
                mode = self.properties.get("mode")
                if name == "fan-level" and mode and prop.value_list == mode.value_list:
                    continue
                options = []
                seen = set()
                for option, value in prop.value_list.items():
                    if value in seen:
                        continue
                    seen.add(value)
                    translated = OPTION_LABELS.get(option.casefold(), option)
                    if option.startswith("MyMode"):
                        translated = "自定义模式 " + option[6:]
                    options.append({"value": option, "label": translated})
                controls.append({**control, "kind": "select", "options": options})
            elif len(prop.value_range) >= 2:
                minimum, maximum = prop.value_range[:2]
                step = prop.value_range[2] if len(prop.value_range) > 2 else 1
                if (
                    all(math.isfinite(v) for v in (minimum, maximum, step))
                    and minimum <= maximum
                    and step > 0
                ):
                    controls.append(
                        {
                            **control,
                            "kind": "range",
                            "min": minimum,
                            "max": maximum,
                            "step": step,
                        }
                    )
        return controls


# 音箱专用动作由小爱云执行，不在 MIoT 能力目录里；AI 工具必须把它们列出来，
# 否则模型会得出「这台音箱只能查状态」的结论（用户实测过）。
SPEAKER_MODEL_PREFIX = "xiaomi.wifispeaker."
SPEAKER_ACTIONS = (
    "speaker_play",
    "speaker_pause",
    "speaker_stop",
    "speaker_volume",
    "speaker_loop",
    "speaker_tts",
    "speaker_url",
)


class MiotToolManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_path = Path(settings.state_dir) / "miot-devices.json"
        self.audit_path = Path(settings.state_dir) / "miot-audit.jsonl"
        self.token_path = Path(settings.state_dir) / "mi-home-token.json"
        self.credentials_path = Path(settings.state_dir) / "mi-home-credentials.json"
        self.devices = self._load_devices()
        self.session: aiohttp.ClientSession | None = None
        self.account: PassTokenAccount | None = None
        self.service: MiIOService | None = None
        self.auth_state = "idle"
        self.last_error = ""
        self.last_discovered_at: str | None = None
        self._spec_types: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self.sampling_errors: dict[str, str] = {}
        self._online_checked_at = 0.0
        self.read_diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
        self._read_retry_after: dict[tuple[str, str], float] = {}
        self._safe_device_metadata: dict[str, dict[str, Any]] = {}
        self._last_read_values: dict[str, dict[str, Any]] = {}
        self._last_read_observed: dict[str, str] = {}
        self._read_next_due: dict[tuple[str, str], float] = {}
        self._policy_cache: dict[
            str, tuple[tuple[int, ...], dict[str, dict[str, Any]]]
        ] = {}
        self._legacy_next_due: dict[str, float] = {}
        self._sampling_cursor = 0
        # 音箱等专用通道的执行器由 Bridge 注入（这里不引 speaker_control，避免循环导入）
        self.extension_handler: Callable[..., Any] | None = None

    def _load_devices(self) -> dict[str, DeviceRecord]:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return {}
        devices: dict[str, DeviceRecord] = {}
        for item in payload.get("devices", []):
            try:
                properties = {
                    name: PropertySpec(**value)
                    for name, value in dict(item.get("properties") or {}).items()
                }
                record = DeviceRecord(
                    key=str(item["key"]),
                    did=str(item["did"]),
                    name=str(item["name"]),
                    model=str(item["model"]),
                    category=str(item["category"]),
                    alias=str(item["alias"]),
                    enabled=bool(item.get("enabled")),
                    properties=properties,
                    services=item.get("services") or [],
                    catalog_controls=item.get("catalog_controls") or [],
                    catalog_actions=item.get("catalog_actions") or {},
                    catalog_events=item.get("catalog_events") or [],
                    cloud_online=item.get("cloud_online"),
                    support_note=str(item.get("support_note") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            devices[record.key] = record
        return devices

    def _save_devices(self) -> None:
        _secure_replace(
            self.config_path,
            {"version": 1, "devices": [asdict(item) for item in self.devices.values()]},
        )

    def public_state(self) -> dict[str, Any]:
        devices = []
        for item in sorted(self.devices.values(), key=lambda value: value.name):
            public = item.public_dict()
            diagnostics = self._init_read_diagnostics(item)
            for prop in public["properties"]:
                prop["read_status"] = dict(diagnostics.get(prop["key"], {}))
                prop.update({key: prop["read_status"][key] for key in POLICY_KEYS})
            devices.append(public)
        return {
            "auth_state": self.auth_state,
            "last_error": self.last_error,
            "last_discovered_at": self.last_discovered_at,
            "safe_categories": sorted(
                {category_label(device.category) for device in self.devices.values()}
            ),
            "max_calls_per_turn": MAX_TOOL_CALLS,
            "devices": devices,
            "audit": self.read_audit(),
        }

    async def close(self) -> None:
        await self._disconnect()

    async def _disconnect(self) -> None:
        if self.session is not None:
            await self.session.close()
        self.session = None
        self.account = None
        self.service = None

    def _load_credentials(self) -> dict[str, str]:
        try:
            payload = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return {}
        if str(payload.get("user_id") or "") != self.settings.mi_user_id:
            return {}
        return {
            key: str(payload.get(key) or "").strip()
            for key in ("user_id", "pass_token", "device_id", "c_user_id")
        }

    def save_browser_credentials(self, data: dict[str, object]) -> None:
        user_id = str(data.get("user_id") or "").strip()
        pass_token = str(data.get("pass_token") or "").strip()
        device_id = str(data.get("account_device_id") or "").strip()
        c_user_id = str(data.get("c_user_id") or "").strip()
        if user_id != self.settings.mi_user_id:
            raise ValueError("米家授权账号与当前音箱账号不一致")
        if not pass_token or not device_id:
            raise ValueError("米家授权没有返回完整凭据")
        _secure_replace(
            self.credentials_path,
            {
                "user_id": user_id,
                "pass_token": pass_token,
                "device_id": device_id,
                "c_user_id": c_user_id,
                "region": self.settings.mi_region,
                "note": "Dedicated xiaomiio credential. Do not commit or share.",
            },
        )
        _secure_replace(
            self.token_path,
            {
                "userId": user_id,
                "passToken": pass_token,
                "deviceId": device_id,
            },
        )
        self.auth_state = "idle"
        self.last_error = ""

    def _seed_token_store(self) -> None:
        overrides = RuntimeConfigStore(self.settings.state_dir).load()
        credentials = self._load_credentials()
        existing: dict[str, Any] = {}
        try:
            existing = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            pass
        if str(existing.get("userId") or "") != self.settings.mi_user_id:
            existing = {}
        existing["userId"] = self.settings.mi_user_id
        existing["deviceId"] = str(
            existing.get("deviceId")
            or credentials.get("device_id")
            or overrides.account_device_id
            or self.settings.mi_account_device_id
            or "android_miot"
        )
        existing["passToken"] = str(
            existing.get("passToken")
            or credentials.get("pass_token")
            or self.settings.mi_pass_token
        )
        _secure_replace(self.token_path, existing)

    async def _connect(self, *, renew: bool = False) -> MiIOService:
        if renew:
            await self._disconnect()
        if self.service is not None:
            return self.service
        self._seed_token_store()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
        store = MiTokenStore(str(self.token_path))
        overrides = RuntimeConfigStore(self.settings.state_dir).load()
        credentials = self._load_credentials()
        self.account = PassTokenAccount(
            self.session,
            credentials.get("user_id") or self.settings.mi_user_id,
            credentials.get("pass_token") or self.settings.mi_pass_token,
            store,
            self.credentials_path,
            self.settings.mi_region,
            credentials.get("c_user_id")
            or overrides.c_user_id
            or self.settings.mi_c_user_id,
            "",
            credentials.get("device_id")
            or overrides.account_device_id
            or self.settings.mi_account_device_id,
        )
        if not await self.account.login("xiaomiio"):
            self.auth_state = "needs_login"
            self.last_error = "需要在连接页完成米家设备授权"
            await self._disconnect()
            raise RuntimeError(self.last_error)
        self.service = MappedMiIOService(self.account, self.settings.mi_region)
        self.auth_state = "connected"
        self.last_error = ""
        return self.service

    async def discover(self) -> dict[str, Any]:
        async with self._lock:
            try:
                service = await self._connect(renew=True)
                raw_devices = await service.device_list(name="full") or []
                self._cache_safe_device_metadata(raw_devices)
                discovered: dict[str, DeviceRecord] = {}
                used_aliases: set[str] = set()
                for raw in raw_devices:
                    model = str(raw.get("model") or "").strip()
                    did = str(raw.get("did") or "").strip()
                    name = str(raw.get("name") or model or "未命名设备").strip()
                    if not model or not did:
                        continue
                    spec_error = ""
                    try:
                        spec = await self._fetch_spec(model)
                    except (aiohttp.ClientError, TimeoutError, ValueError):
                        spec = {}
                        spec_error = "设备能力描述暂时无法获取，请稍后重新发现"
                    record = self._record_from_spec(did, name, model, spec)
                    previous = self.devices.get(record.key)
                    if previous:
                        record.alias = previous.alias
                        record.enabled = previous.enabled
                    else:
                        record.enabled = True
                    if spec_error and previous:
                        record = previous
                    online = raw.get("isOnline")
                    record.cloud_online = online if isinstance(online, bool) else None
                    record.support_note = spec_error or record.support_note
                    record.alias = self._unique_alias(record.alias, used_aliases)
                    used_aliases.add(record.alias.casefold())
                    discovered[record.key] = record
                self.devices = discovered
                self._read_next_due.clear()
                self._read_retry_after.clear()
                self._legacy_next_due.clear()
                self._policy_cache.clear()
                self.last_discovered_at = datetime.now(UTC).isoformat()
                self._online_checked_at = time.monotonic()
                self.last_error = ""
                self._save_devices()
            except Exception as exc:
                if self.auth_state != "needs_login":
                    self.auth_state = "error"
                    self.last_error = str(exc)
                raise
        return self.public_state()

    @staticmethod
    def _unique_alias(alias: str, used: set[str]) -> str:
        if alias.casefold() not in used:
            return alias
        number = 2
        while f"{alias}{number}".casefold() in used:
            number += 1
        return f"{alias}{number}"

    async def _fetch_spec(self, model: str) -> dict[str, Any]:
        assert self.session is not None
        if self._spec_types is None:
            async with self.session.get(SPEC_INSTANCES_URL) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            self._spec_types = {
                str(item.get("model")): str(item.get("type"))
                for item in payload.get("instances", [])
                if item.get("model") and item.get("type")
            }
        spec_type = self._spec_types.get(model, "")
        if not spec_type:
            return {}
        async with self.session.get(
            SPEC_INSTANCE_URL, params={"type": spec_type}
        ) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    @staticmethod
    def _record_from_spec(
        did: str, name: str, model: str, spec: dict[str, Any]
    ) -> DeviceRecord:
        category = _urn_name(spec.get("type"), "device") or "unknown"
        catalog = build_catalog(spec, category)
        properties = {
            key: PropertySpec(**prop) for key, prop in catalog["properties"].items()
        }
        key = hashlib.sha256(f"{did}\0{model}".encode()).hexdigest()[:16]
        return DeviceRecord(
            key=key,
            did=did,
            name=name,
            model=model,
            category=category,
            alias=name[:32],
            properties=properties,
            services=catalog["services"],
            catalog_controls=catalog["controls"],
            catalog_actions=catalog["actions"],
            catalog_events=catalog["events"],
            support_note=(
                "该型号尚未公开 MIoT 能力描述"
                if not spec
                else "该型号仅公开设备信息；其他功能需要专用适配"
                if all(p.service == "device-information" for p in properties.values())
                else ""
            ),
        )

    def update_device(self, key: str, *, alias: str, enabled: bool) -> dict[str, Any]:
        device = self.devices.get(key)
        if device is None:
            raise ValueError("设备不存在，请重新发现设备")
        value = alias.strip()
        if not value or len(value) > 32:
            raise ValueError("设备别名长度必须为 1 到 32 个字符")
        if enabled and any(
            item.key != key
            and item.enabled
            and item.alias.casefold() == value.casefold()
            for item in self.devices.values()
        ):
            raise ValueError("已启用设备的别名不能重复")
        was_enabled = device.enabled
        device.alias = value
        device.enabled = enabled
        if enabled and not was_enabled:
            for schedule in (self._read_next_due, self._read_retry_after):
                for identity in list(schedule):
                    if identity[0] == device.key:
                        schedule.pop(identity, None)
            self._legacy_next_due.pop(device.key, None)
        self._save_devices()
        return device.public_dict()

    def tool_definitions(self) -> list[dict[str, Any]]:
        enabled = [item for item in self.devices.values() if item.enabled]
        if not enabled:
            return []
        aliases = sorted(item.alias for item in enabled)
        actions = sorted({action for item in enabled for action in item.actions()})
        speakers = [item for item in enabled if item.model.startswith(SPEAKER_MODEL_PREFIX)]
        if speakers:
            actions = sorted({*actions, *SPEAKER_ACTIONS})
        capability_lines = []
        for device in sorted(enabled, key=lambda item: item.alias):
            values = []
            for name, prop in device.properties.items():
                if name not in PROPERTY_ACTIONS or not prop.writable or prop.sensitive:
                    continue
                if prop.value_list:
                    values.append(f"{name}={','.join(prop.value_list)}")
                elif prop.value_range:
                    values.append(
                        f"{name}={prop.value_range[0]:g}..{prop.value_range[1]:g}"
                    )
            device_actions = list(device.actions())
            if device.model.startswith(SPEAKER_MODEL_PREFIX):
                # 音箱的播放/播报走小爱云，不在 MIoT 目录里，但模型必须看得见
                device_actions.extend(SPEAKER_ACTIONS)
                values.append("speaker_tts=要播报的文字; speaker_volume=0..100; speaker_loop=0|1")
            capability_lines.append(
                f"{device.alias}({category_label(device.category)}): "
                f"动作={','.join(device_actions)}; "
                f"值={'; '.join(values) or '无'}"
            )
        return [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": (
                        "查询或控制用户已授权的米家设备。询问某台家电的当前开关、模式或"
                        "环境读数时用 get_state；明确要求操作时使用该设备支持的设置动作。"
                        "常识问答、假设问题、含糊的感受不调用控制动作；目标不唯一先问清。"
                        "控制结果会由后端校验并回读，outcome=confirmed 才确认生效；"
                        "unconfirmed 只表示已发送但未确认，不自动重复写入。"
                        "speaker_ 开头的动作只能用在音箱上：用户明确要求让音箱说话、"
                        "朗读或播报时用 speaker_tts（value 是要播报的文字），"
                        "调音量用 speaker_volume（value 为 0-100 的整数），"
                        "播放控制用 speaker_play/speaker_pause/speaker_stop；"
                        "用户没有要求出声时不要调用这些动作。"
                        "已授权设备能力：" + " | ".join(capability_lines)
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "device": {
                                "type": "string",
                                "enum": aliases,
                                "description": "设备别名",
                            },
                            "action": {
                                "type": "string",
                                "enum": actions,
                                "description": "get_state 查询当前状态，turn_on/turn_off 开关，set_brightness 亮度，set_color_temperature 色温，set_temperature 空调目标温度（不是室温），set_mode 模式，set_speed 风速；只能选择该设备能力列表中的动作",
                            },
                            "value": {
                                "type": "string",
                                "description": "设置动作所需的值；开关和查询动作省略",
                            },
                        },
                        "required": ["device", "action"],
                    },
                },
            }
        ]

    async def execute(
        self,
        name: str,
        arguments: object,
        *,
        device_key: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if name != TOOL_NAME:
            return {"ok": False, "error": "不支持的工具"}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"ok": False, "error": "工具参数不是有效 JSON"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "工具参数必须是对象"}
        if set(arguments) - {"device", "action", "value"}:
            return {"ok": False, "error": "工具参数包含未允许字段"}
        alias = str(arguments.get("device") or "").strip()
        action = str(arguments.get("action") or "").strip()
        value = arguments.get("value")
        device = (
            self.devices.get(device_key)
            if device_key is not None
            else next(
                (
                    item
                    for item in self.devices.values()
                    if item.enabled and item.alias.casefold() == alias.casefold()
                ),
                None,
            )
        )
        if device is None or not device.enabled:
            return await self._audit_result(alias, action, False, "设备未授权")
        alias = device.alias
        if device_key is not None and action.startswith(("prop_", "action_")):
            return await self._execute_capability(device, action, value, confirmed)
        if action.startswith("speaker_"):
            # 音箱的播放/播报走小爱云，不经过 MIoT 目录
            if not device.model.startswith(SPEAKER_MODEL_PREFIX):
                return await self._audit_result(alias, action, False, "该设备不是音箱")
            if action not in SPEAKER_ACTIONS:
                return await self._audit_result(alias, action, False, "不支持的小爱控制操作")
            if self.extension_handler is None:
                return await self._audit_result(alias, action, False, "音箱控制未就绪")
            try:
                outcome = await self.extension_handler(device, action, value)
            except Exception as exc:  # noqa: BLE001 - 上游错误不能带凭据
                return await self._audit_result(alias, action, False, str(exc))
            if not isinstance(outcome, dict):
                return await self._audit_result(alias, action, False, "音箱没有返回结果")
            ok = bool(outcome.get("ok"))
            # 专用动作没有可回读的属性，用 outcome/message 让模型如实描述
            return await self._audit_result(
                alias,
                action,
                ok,
                "" if ok else str(outcome.get("message") or outcome.get("error") or "音箱拒绝执行"),
                {
                    "ok": ok,
                    "device": alias,
                    "action": action,
                    "outcome": outcome.get("outcome") or ("confirmed" if ok else "failed"),
                    "message": str(outcome.get("message") or "")[:300],
                },
            )
        if action not in device.actions():
            return await self._audit_result(alias, action, False, "该设备不支持此动作")
        write_started = False
        observed: dict[str, Any] = {}
        observed_at: str | None = None
        try:
            async with self._lock:
                self._check_current_device(device, action)
                prop_name, wanted = (
                    self._validated_value(device, action, value)
                    if action != "get_state"
                    else ("", None)
                )
                service = await self._connect()
                self._check_current_device(device, action)
                if action == "get_state":
                    result = await self._read_state(service, device, force=True)
                    result.update(
                        outcome="confirmed",
                        message="状态已更新",
                        readback=result["state"],
                        observed_at=datetime.now(UTC).isoformat(),
                    )
                else:
                    prop = device.properties[prop_name]
                    if not prop.readable:
                        raise ValueError("该属性不支持状态确认")
                    write_started = True
                    codes = await service.miot_set_props(
                        device.did, [(prop.siid, prop.piid, wanted)]
                    )
                    if not codes or codes[0] != 0:
                        write_started = False
                        raise RuntimeError(
                            f"米家写入失败，错误码 {codes[0] if codes else -1}"
                        )
                    actual = None
                    confirmed = False
                    direct_readback: dict[str, Any] = {}
                    direct_observed_at: str | None = None
                    full_read = False
                    for attempt in range(3):
                        if attempt:
                            await asyncio.sleep(0.6 * attempt)
                        try:
                            async with asyncio.timeout(5):
                                values = await service.miot_get_props(
                                    device.did, [(prop.siid, prop.piid)]
                                )
                        except (TimeoutError, RuntimeError, aiohttp.ClientError):
                            continue
                        actual = values[0] if values else None
                        if actual is not None:
                            observed = {prop_name: self._display_value(prop, actual)}
                            observed_at = datetime.now(UTC).isoformat()
                            direct_readback = dict(observed)
                            direct_observed_at = observed_at
                        if actual is not None and self._values_equal(actual, wanted):
                            confirmed = True
                            break
                    try:
                        async with asyncio.timeout(5):
                            full = await self._read_state(service, device, force=True)
                        observed = full["state"]
                        observed_at = (
                            full.get("observed_at") or datetime.now(UTC).isoformat()
                        )
                        full_read = True
                    except (TimeoutError, RuntimeError, aiohttp.ClientError):
                        pass
                    fresh_target = self._merge_control_observation(
                        device,
                        prop,
                        observed,
                        direct_readback,
                        direct_observed_at,
                        full_read=full_read,
                    )
                    confirmed = fresh_target and observed.get(
                        prop_name
                    ) == self._display_value(prop, wanted)
                    result = {
                        "ok": confirmed,
                        "device": device.alias,
                        "action": action,
                        "state": observed.get(prop_name),
                        "readback": observed,
                        "observed_at": observed_at,
                        "outcome": "confirmed" if confirmed else "unconfirmed",
                        "message": "操作已生效"
                        if confirmed
                        else "指令已发送，暂未确认生效，请刷新设备状态",
                    }
        except Exception as exc:  # noqa: BLE001 - device API execution boundary
            result = {
                "ok": False,
                "device": alias,
                "action": action,
                "outcome": "unconfirmed" if write_started else "failed",
                "message": "指令结果尚未确认，请刷新设备状态"
                if write_started
                else scrub(exc)[:200],
                "readback": observed,
                "observed_at": observed_at,
            }
            return await self._audit_result(
                alias, action, False, result["message"], result
            )
        return await self._audit_result(
            alias,
            action,
            result["ok"],
            "" if result["ok"] else result["message"],
            result,
        )

    @staticmethod
    def _validate_property_value(prop: PropertySpec, value: object) -> Any:
        if prop.sensitive or prop.blocked_reason:
            raise ValueError(prop.blocked_reason or "敏感字段不允许读写")
        if prop.format == "bool":
            if not isinstance(value, bool):
                raise ValueError("开关值必须是布尔值")
            return value
        if prop.value_list:
            for label, raw in prop.value_list.items():
                if (
                    str(value).casefold() == label.casefold()
                    or type(value) is type(raw)
                    and value == raw
                ):
                    return raw
            raise ValueError("请选择设备提供的选项")
        if prop.format == "string":
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("文本长度不能超过 500 字")
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float, str))
            or len(prop.value_range) < 2
        ):
            raise ValueError("缺少有效值或设备范围定义")
        number = float(value)
        lo, hi = prop.value_range[:2]
        step = prop.value_range[2] if len(prop.value_range) > 2 else 1
        if not math.isfinite(number) or not lo <= number <= hi or step <= 0:
            raise ValueError(f"数值必须在 {lo:g} 到 {hi:g} 之间")
        steps = (number - lo) / step
        if not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"数值必须按 {step:g} 递增")
        if prop.format.startswith(("int", "uint")):
            if not number.is_integer():
                raise ValueError("数值必须是整数")
            return int(number)
        return number

    async def _execute_capability(
        self, device: DeviceRecord, action: str, value: object, confirmed: bool
    ) -> dict[str, Any]:
        control = next(
            (c for c in device.catalog_controls if c["action"] == action), None
        )
        operation = control or device.catalog_actions.get(action)
        result: dict[str, Any] = {
            "device": device.alias,
            "action": action,
            "ok": False,
            "readback": {},
        }
        sent = False
        try:
            async with self._lock:
                if self.devices.get(device.key) is not device or not device.enabled:
                    raise ValueError("设备已停用或设备目录已更新")
                if not operation or not operation.get("enabled"):
                    raise ValueError(
                        (operation or {}).get("reason") or "该能力暂不支持执行"
                    )
                if operation.get("requires_confirmation") and not confirmed:
                    raise ValueError("此操作需要在设备面板确认后执行")
                prop = device.properties.get(control["property"]) if control else None
                if prop:
                    wanted = self._validate_property_value(prop, value)
                    if not prop.writable:
                        raise ValueError("该属性不可写")
                else:
                    inputs = operation.get("inputs") or []
                    arguments = {} if value is None else value
                    if not isinstance(arguments, dict) or set(arguments) != {
                        p["key"] for p in inputs
                    }:
                        raise ValueError("动作参数不完整或包含额外参数")
                    wanted = [
                        self._validate_property_value(
                            device.properties[p["property"]], arguments[p["key"]]
                        )
                        for p in inputs
                    ]
                service = await self._connect()
                if self.devices.get(device.key) is not device or not device.enabled:
                    raise ValueError("设备授权已变化")
                sent = True
                if prop:
                    codes = await service.miot_set_props(
                        device.did, [(prop.siid, prop.piid, wanted)]
                    )
                    code = codes[0] if codes else -1
                else:
                    code = await service.miot_action(
                        device.did, (operation["siid"], operation["aiid"]), wanted
                    )
                if code != 0:
                    sent = False
                    raise ValueError(f"设备未接受指令（{code}）")
                outcome = "accepted"
                direct_readback: dict[str, Any] = {}
                direct_observed_at: str | None = None
                full_read = False
                if prop and prop.readable:
                    outcome = "unconfirmed"
                    for attempt in range(3):
                        if attempt:
                            await asyncio.sleep(0.6 * attempt)
                        try:
                            async with asyncio.timeout(5):
                                values = await service.miot_get_props(
                                    device.did, [(prop.siid, prop.piid)]
                                )
                            if values and values[0] is not None:
                                result["readback"] = {
                                    prop.name: self._display_value(prop, values[0])
                                }
                                result["observed_at"] = datetime.now(UTC).isoformat()
                                direct_readback = dict(result["readback"])
                                direct_observed_at = result["observed_at"]
                                if self._values_equal(values[0], wanted):
                                    outcome = "confirmed"
                                    break
                        except (TimeoutError, RuntimeError, aiohttp.ClientError):
                            continue
                try:
                    async with asyncio.timeout(8):
                        full = await self._read_state(service, device, force=True)
                    result["readback"] = full["state"]
                    result["observed_at"] = (
                        full.get("observed_at") or datetime.now(UTC).isoformat()
                    )
                    full_read = True
                except (TimeoutError, RuntimeError, aiohttp.ClientError):
                    pass
                if prop and prop.readable:
                    readback = result.get("readback") or {}
                    fresh_target = self._merge_control_observation(
                        device,
                        prop,
                        readback,
                        direct_readback,
                        direct_observed_at,
                        full_read=full_read,
                    )
                    if readback:
                        result["readback"] = readback
                    outcome = (
                        "confirmed"
                        if fresh_target
                        and readback.get(prop.name) == self._display_value(prop, wanted)
                        else "unconfirmed"
                    )
                result.update(
                    ok=outcome != "unconfirmed",
                    outcome=outcome,
                    message={
                        "confirmed": "操作已生效",
                        "accepted": "设备已接收指令，执行结果需在设备上确认",
                        "unconfirmed": "指令已发送，状态暂未确认，请刷新",
                    }[outcome],
                )
        except Exception as exc:  # noqa: BLE001 - cloud execution boundary
            result.update(
                outcome="unconfirmed" if sent else "failed",
                message="指令结果暂未确认，请刷新设备状态"
                if sent
                else scrub(exc)[:200],
            )
        return await self._audit_result(
            device.alias,
            action,
            result["ok"],
            "" if result["ok"] else result["message"],
            result,
        )

    def _check_current_device(self, device: DeviceRecord, action: str) -> None:
        if self.devices.get(device.key) is not device or not device.enabled:
            raise ValueError("设备授权或目录已变化，请刷新设备列表")
        if action not in device.actions():
            raise ValueError("该设备不支持此动作")

    def _merge_control_observation(
        self,
        device: DeviceRecord,
        prop: PropertySpec,
        readback: dict[str, Any],
        direct: dict[str, Any],
        direct_at: str | None,
        *,
        full_read: bool,
    ) -> bool:
        """Confirm only a post-write observation, never a retained stale value."""
        diagnostics = self._init_read_diagnostics(device)
        detail = diagnostics[prop.name]
        if (
            full_read
            and prop.name in readback
            and detail.get("status") == "ok"
            and not detail.get("cached")
        ):
            # A later fresh read supersedes a targeted observation, including a
            # conflicting value changed by the physical device or another client.
            return True
        if not direct_at or prop.name not in direct:
            return False
        readback[prop.name] = direct[prop.name]
        diagnostics[prop.name] = {
            "status": "ok",
            "code": 0,
            "source": "miot.cloud",
            "attempted_at": direct_at,
            "observed_at": direct_at,
            "message": "指令后的单独回读成功；随后批量读取未返回该字段",
            "cached": False,
        }
        self._last_read_values.setdefault(device.key, {})[prop.name] = direct[prop.name]
        self._read_next_due[(device.key, prop.name)] = (
            time.monotonic()
            + self._field_policies(device)[prop.name]["refresh_seconds"]
        )
        return True

    async def snapshot_enabled_states(
        self,
        *,
        force: bool = False,
        budget_seconds: float = 40,
        device_keys: set[str] | None = None,
    ) -> list[tuple[DeviceRecord, dict[str, Any]]]:
        """Read serially within one budget, retaining completed device samples.

        The cloud client shares property diagnostics, so reads must not overlap.
        Start after the last attempted device when a cycle runs out of time.
        External cancellation still propagates to permit prompt shutdown.
        """
        if budget_seconds <= 0:
            raise ValueError("采集时间预算必须大于零")
        self.sampling_errors = {}
        enabled = [
            device
            for device in self.devices.values()
            if device.enabled and (device_keys is None or device.key in device_keys)
        ]
        if not enabled:
            return []
        start = self._sampling_cursor % len(enabled)
        ordered = enabled[start:] + enabled[:start]
        snapshots: list[tuple[DeviceRecord, dict[str, Any]]] = []
        try:
            async with asyncio.timeout(budget_seconds), self._lock:
                service = await self._connect()
                if time.monotonic() - self._online_checked_at >= 300 and hasattr(
                    service, "device_list"
                ):
                    try:
                        async with asyncio.timeout(5):
                            online_rows = await service.device_list(name="full")
                        self._cache_safe_device_metadata(online_rows or [])
                        by_did = {
                            str(row.get("did")): row.get("isOnline")
                            for row in online_rows or []
                        }
                        for record in self.devices.values():
                            if isinstance(by_did.get(record.did), bool):
                                record.cloud_online = by_did[record.did]
                    except (aiohttp.ClientError, RuntimeError, TimeoutError):
                        pass
                    self._online_checked_at = time.monotonic()
                for index, device in enumerate(ordered):
                    self._sampling_cursor = (start + index + 1) % len(enabled)
                    if not device.enabled:
                        continue
                    self._init_read_diagnostics(device)
                    if device.cloud_online is False or not any(
                        p.readable for p in device.properties.values()
                    ):
                        continue
                    try:
                        async with asyncio.timeout(6):
                            result = await self._read_state(service, device, force=force)
                        result.setdefault("observed_at", datetime.now(UTC).isoformat())
                        if device.enabled and result.get("fresh_fields"):
                            snapshots.append((device, result))
                    except Exception as exc:  # noqa: BLE001 - isolate one device
                        self.sampling_errors[device.key] = "设备状态暂时无法读取"
                        LOGGER.warning(
                            "MIoT state sampling failed device=%s error=%s",
                            device.alias,
                            scrub(exc),
                        )
                # Even complete cycles rotate which device receives the first slot.
                self._sampling_cursor = (start + 1) % len(enabled)
        except TimeoutError:
            self.sampling_errors["cycle"] = "本轮采集超时，已保留成功数据，其余设备稍后重试"
        return snapshots

    async def _read_state(
        self, service: MiIOService, device: DeviceRecord, *, force: bool = True
    ) -> dict[str, Any]:
        diagnostics = self._init_read_diagnostics(device)
        policies = self._field_policies(device)
        now = time.monotonic()
        deadline = time.monotonic() + 5
        readable = [
            prop
            for prop in device.properties.values()
            if prop.readable and not prop.sensitive and prop.siid > 0
        ]
        pending = [
            prop
            for prop in readable
            if force
            or now
            >= max(
                self._read_retry_after.get((device.key, prop.name), 0),
                self._read_next_due.get((device.key, prop.name), 0),
            )
        ]
        pending_names = {prop.name for prop in pending}
        deferred = {prop.name for prop in readable if prop.name not in pending_names}
        for name in deferred:
            if (
                name in self._last_read_values.get(device.key, {})
                and diagnostics[name].get("status") == "ok"
            ):
                diagnostics[name].update(
                    cached=True, message="使用有效缓存，尚未到计划刷新时间"
                )
        fresh_fields: set[str] = set()
        attempted_any = bool(pending)
        state: dict[str, Any] = {}
        for offset in range(0, len(pending), 10):
            batch = pending[offset : offset + 10]
            attempted = datetime.now(UTC).isoformat()
            batch_values: list[Any] = []
            error_message = ""
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                async with asyncio.timeout(min(2.0, remaining)):
                    received = await service.miot_get_props(
                        device.did, [(prop.siid, prop.piid) for prop in batch]
                    )
                if not isinstance(received, (list, tuple)):
                    raise TypeError("属性读取返回格式无效")
                batch_values = list(received)
            except Exception:  # noqa: BLE001 - keep earlier batches on partial cloud failures
                error_message = "本批属性暂时无法读取，已保留其他成功读数"
            mapped = getattr(service, "last_property_reads", {})
            if not isinstance(mapped, dict):
                mapped = {}
            for index, prop in enumerate(batch):
                value = batch_values[index] if index < len(batch_values) else None
                previous_observed = diagnostics[prop.name].get("observed_at")
                previous_source_observed = diagnostics[prop.name].get("source_updated_at")
                previous_value = self._last_read_values.get(device.key, {}).get(prop.name)
                detail = dict(mapped.get((device.did, prop.siid, prop.piid), {}))
                detail.setdefault("attempted_at", attempted)
                detail.setdefault("source", "miot.cloud")
                detail["cached"] = False
                if previous_observed:
                    detail.setdefault("observed_at", previous_observed)
                if value is not None and not error_message:
                    state[prop.name] = self._display_value(prop, value)
                    source_observed = detail.get("source_updated_at")
                    same_source_sample = bool(
                        source_observed
                        and source_observed == previous_source_observed
                        and previous_value == state[prop.name]
                    )
                    detail["observed_at"] = attempted
                    if source_observed:
                        detail["source_updated_at"] = source_observed
                        try:
                            detail["source_age_seconds"] = max(
                                0,
                                round((datetime.now(UTC) - datetime.fromisoformat(source_observed)).total_seconds()),
                            )
                        except (TypeError, ValueError):
                            detail.pop("source_age_seconds", None)
                        detail["source_sample_cached"] = same_source_sample
                        if same_source_sample:
                            detail["message"] = "米家云读取成功；云端属性时间和值未变化，实时性未核实"
                        elif source_observed == previous_source_observed:
                            detail["message"] = "米家云返回值已变化，但云端属性时间未变化，实时性未核实"
                        else:
                            detail["message"] = "米家云读取成功；云端属性更新时间不代表传感器采样时间"
                    detail.update(
                        status="ok", code=0, message=detail.get("message", "读取成功")
                    )
                    self._read_retry_after.pop((device.key, prop.name), None)
                    fresh_fields.add(prop.name)
                    self._read_next_due[(device.key, prop.name)] = (
                        time.monotonic() + policies[prop.name]["refresh_seconds"]
                    )
                else:
                    detail.setdefault("status", "unavailable")
                    detail.setdefault("message", "云端没有返回该属性")
                    if error_message:
                        detail.update(
                            status="unavailable",
                            message=error_message,
                            attempted_at=attempted,
                        )
                        detail.pop("code", None)
                    if previous_source_observed:
                        detail["source_updated_at"] = previous_source_observed
                    if detail["status"] == "unsupported":
                        self._read_retry_after[(device.key, prop.name)] = (
                            time.monotonic() + 300
                        )
                    self._read_next_due[(device.key, prop.name)] = time.monotonic() + (
                        300
                        if detail["status"] == "unsupported"
                        else min(300, max(15, policies[prop.name]["refresh_seconds"]))
                    )
                diagnostics[prop.name] = detail

        # These two observed firmware aliases fail while the standard serial
        # property succeeds. Reuse the named field, retaining explicit provenance.
        if device.model in {"yeelink.light.lamp27", "miwear.watch.p65"}:
            if (
                "serial-no" in device.properties
                and "serial-no" not in state
                and "serial-number" in state
            ):
                state["serial-no"] = state["serial-number"]
                diagnostics["serial-no"] = {
                    **diagnostics["serial-number"],
                    "source": "miot.alias",
                    "source_field": "serial-number",
                    "message": "使用设备标准序列号字段兼容映射",
                }
                fresh_fields.add("serial-no")
                self._read_next_due[(device.key, "serial-no")] = (
                    self._read_next_due.get((device.key, "serial-number"), now)
                )
            elif (
                "serial-no" in diagnostics
                and diagnostics["serial-no"].get("source") == "miot.alias"
                and "serial-no" not in deferred
            ):
                diagnostics["serial-no"].update(
                    status="unavailable",
                    message="标准序列号来源暂时不可用，保留上次成功时间",
                )
        if device.model in {
            "chuangmi.ir.v2",
            "chuangmi.remote.v2",
            "chuangmi.remote.h102a03",
        }:
            if force or now >= self._legacy_next_due.get(device.key, 0):
                attempted_any = True
                before = {
                    name: detail.get("observed_at")
                    for name, detail in diagnostics.items()
                }
                await self._read_legacy_info(
                    service, device, state, diagnostics, deadline
                )
                policies = self._field_policies(device)
                legacy_fresh = {
                    name
                    for name in state
                    if diagnostics.get(name, {}).get("source") == "miio.cloud"
                    and diagnostics[name].get("observed_at") != before.get(name)
                }
                fresh_fields.update(legacy_fresh)
                delay = min(
                    (policies[name]["refresh_seconds"] for name in legacy_fresh),
                    default=300,
                )
                self._legacy_next_due[device.key] = time.monotonic() + max(15, delay)
                for name in legacy_fresh:
                    diagnostics[name]["cached"] = False
                    self._read_next_due[(device.key, name)] = (
                        time.monotonic() + policies[name]["refresh_seconds"]
                    )
            else:
                deferred.update(
                    name for name, prop in device.properties.items() if prop.siid == 0
                )
        # Safe inventory metadata is a separately labelled fallback, not a claim
        # that an unsupported property suddenly became live MIoT telemetry.
        metadata = self._safe_device_metadata.get(device.did, {})
        for name in ("model", "firmware-revision"):
            if (
                name in device.properties
                and name not in state
                and metadata.get(name)
                and name not in deferred
            ):
                state[name] = metadata[name]
                diagnostics[name] = {
                    "status": "ok",
                    "message": "来自米家设备目录信息",
                    "source": "xiaomi.device_list",
                    "attempted_at": metadata["observed_at"],
                    "observed_at": metadata["observed_at"],
                    "cached": time.monotonic() - metadata.get("fetched", 0) > 5,
                }
                if time.monotonic() - metadata.get("fetched", 0) <= 5:
                    fresh_fields.add(name)
                self._read_next_due[(device.key, name)] = (
                    time.monotonic() + policies[name]["refresh_seconds"]
                )
        if not fresh_fields and attempted_any and not state:
            raise RuntimeError("设备没有返回可读状态")
        cached = self._last_read_values.setdefault(device.key, {})
        for name, value in state.items():
            if diagnostics.get(name, {}).get("status") == "ok":
                cached[name] = value
        for name, value in cached.items():
            prop = device.properties.get(name)
            if name in state or prop is None or prop.sensitive or not prop.readable:
                continue
            state[name] = value
            detail = diagnostics[name]
            detail["cached"] = True
            if name in deferred and detail.get("status") == "ok":
                detail["message"] = "使用有效缓存，尚未到计划刷新时间"
            else:
                detail.update(
                    status="stale",
                    message=detail.get("message", "本次读取失败").removesuffix(
                        "；显示上次读数"
                    )
                    + "；显示上次读数",
                )
        self._init_read_diagnostics(device)
        if not state:
            raise RuntimeError("设备没有返回可读状态")
        read_at = datetime.now(UTC).isoformat()
        result_observed = read_at if fresh_fields else self._last_read_observed.get(device.key, read_at)
        self._last_read_observed[device.key] = result_observed
        source_observed = max(
            (diagnostics[name].get("source_updated_at", "") for name in state), default=""
        )
        return {
            "ok": True,
            "device": device.alias,
            "action": "get_state",
            "state": state,
            "observed_at": result_observed,
            "source_observed_at": source_observed or None,
            "field_status": {
                name: {
                    key: detail[key]
                    for key in (
                        "status", "message", "source", "cached", "observed_at",
                        "attempted_at", "source_updated_at", "source_sample_cached",
                    )
                    if key in detail
                }
                for name in state
                if (detail := diagnostics.get(name))
            },
            "fresh_fields": sorted(fresh_fields),
            "cached_only": not fresh_fields,
            "ttl_seconds": self.device_ttl_seconds(device),
        }

    def _field_policies(self, device: DeviceRecord) -> dict[str, dict[str, Any]]:
        fingerprint = tuple(
            hash(
                (
                    id(prop),
                    name,
                    prop.format,
                    prop.readable,
                    prop.writable,
                    prop.sensitive,
                    prop.unit,
                    prop.label,
                    prop.service,
                    len(prop.value_list),
                )
            )
            for name, prop in device.properties.items()
        )
        cached = self._policy_cache.get(device.key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        fields = build_field_catalog(
            {},
            device.public_dict()["properties"],
            source="miot.cloud",
        )
        policies = {
            field["key"]: {
                **field,
                **policy_for_field(field, self.settings.home_state_poll_seconds),
            }
            for field in fields
        }
        self._policy_cache[device.key] = (fingerprint, policies)
        return policies

    def device_ttl_seconds(self, device: DeviceRecord) -> int:
        """Resource lifetime follows its actually available fields, not a fast default."""
        policies = self._field_policies(device)
        diagnostics = self.read_diagnostics.get(device.key, {})
        intervals = [
            policy["refresh_seconds"]
            for name, policy in policies.items()
            if policy["refresh_seconds"] > 0
            and diagnostics.get(name, {}).get("status") == "ok"
        ]
        interval = min(intervals, default=self.settings.home_state_poll_seconds)
        return max(30, min(86400, interval * 3))

    def _init_read_diagnostics(self, device: DeviceRecord) -> dict[str, dict[str, Any]]:
        diagnostics = self.read_diagnostics.setdefault(device.key, {})
        policies = self._field_policies(device)
        for name, prop in device.properties.items():
            detail = diagnostics.setdefault(
                name,
                {
                    "status": "unavailable",
                    "message": "等待首次读取",
                    "source": "miot.cloud",
                    "attempted_at": None,
                },
            )
            if prop.sensitive:
                detail.update(status="sensitive", message="凭据类字段不读取或展示")
            elif not prop.readable:
                detail.update(
                    status="not_readable",
                    message="规格未开放直接读取；事件或动作输出需相应接入",
                )
            elif device.cloud_online is False:
                detail.update(status="offline", message="米家显示设备离线")
            elif detail.get("status") == "offline":
                detail.update(status="unavailable", message="设备已上线，等待刷新读数")
            policy = policy_for_field(
                {
                    **policies[name],
                    **detail,
                    "readable": prop.readable,
                    "sensitive": prop.sensitive,
                },
                self.settings.home_state_poll_seconds,
            )
            detail.update(policy)
            detail.setdefault("cached", False)
            due = max(
                self._read_next_due.get((device.key, name), 0),
                self._read_retry_after.get((device.key, name), 0),
            )
            detail["next_refresh_at"] = (
                (
                    datetime.now(UTC)
                    + timedelta(seconds=max(0, due - time.monotonic()))
                ).isoformat()
                if policy["refresh_seconds"] and device.cloud_online is not False
                else None
            )
        return diagnostics

    def _cache_safe_device_metadata(self, rows: list[dict[str, Any]]) -> None:
        observed_at = datetime.now(UTC).isoformat()
        for row in rows:
            if not isinstance(row, dict):
                continue
            did = str(row.get("did") or "")
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            safe = {"observed_at": observed_at, "fetched": time.monotonic()}
            for key, value in (
                ("model", row.get("model")),
                ("firmware-revision", extra.get("fw_version")),
            ):
                if isinstance(value, str) and value and len(value) <= 200:
                    safe[key] = value
            self._safe_device_metadata[did] = safe

    async def _read_legacy_info(
        self,
        service: MiIOService,
        device: DeviceRecord,
        state: dict[str, Any],
        diagnostics: dict[str, dict[str, Any]],
        deadline: float,
    ) -> None:
        attempted = datetime.now(UTC).isoformat()
        expected = {"model", "firmware-revision"} | {
            name for name in device.properties if name.startswith("legacy-")
        }
        for name in expected:
            if name in diagnostics and name not in state:
                diagnostics[name].update(
                    status="unavailable",
                    source="miio.cloud",
                    attempted_at=attempted,
                    message="兼容接口尚未返回此字段，保留上次成功时间",
                )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            async with asyncio.timeout(min(2.0, remaining)):
                info = await service.home_request(device.did, "miIO.info", [])
            for _ in range(4):
                if isinstance(info, list) and len(info) == 1:
                    info = info[0]
                elif isinstance(info, dict) and "result" in info:
                    if info.get("error") or ("code" in info and info["code"] != 0):
                        raise RuntimeError("miIO info failed")
                    info = info["result"]
                else:
                    break
            if (
                not isinstance(info, dict)
                or info.get("error")
                or ("code" in info and info["code"] != 0)
            ):
                raise RuntimeError("miIO info unavailable")
            mappings = [
                ("model", "model", "设备型号", "string"),
                ("firmware-revision", "fw_ver", "固件版本", "string"),
                ("legacy-hardware-version", "hw_ver", "硬件版本", "string"),
                (
                    "legacy-wifi-firmware-version",
                    "wifi_fw_ver",
                    "无线模块固件",
                    "string",
                ),
                ("legacy-runtime-counter", "life", "运行计数（设备原值）", "uint32"),
                ("legacy-free-memory", "mmfree", "空闲内存（设备原值）", "uint32"),
            ]
            for name, source_key, label, fmt in mappings:
                value = info.get(source_key)
                if (
                    fmt == "string"
                    and (not isinstance(value, str) or not value or len(value) > 200)
                ) or (fmt != "string" and (type(value) is not int or value < 0)):
                    continue
                if name not in device.properties:
                    device.properties[name] = PropertySpec(
                        siid=0,
                        piid=0,
                        name=name,
                        format=fmt,
                        readable=True,
                        writable=False,
                        label=label,
                        service="legacy-device-information",
                        service_label="设备运行信息",
                    )
                state[name] = value
                diagnostics[name] = {
                    "status": "ok",
                    "message": "已通过兼容设备信息接口读取",
                    "source": "miio.cloud",
                    "source_field": source_key,
                    "attempted_at": attempted,
                    "observed_at": attempted,
                }
        except Exception:  # noqa: BLE001 - legacy failures cannot discard MIoT successes
            for name in expected:
                if name in diagnostics and name not in state:
                    diagnostics[name].update(
                        status="unavailable",
                        source="miio.cloud",
                        attempted_at=attempted,
                        message="兼容设备信息接口暂时不可用，保留上次成功时间",
                    )

    @staticmethod
    def _validated_value(
        device: DeviceRecord, action: str, value: object
    ) -> tuple[str, Any]:
        if action in {"turn_on", "turn_off"}:
            prop = device.properties.get("on")
            if prop is None or not prop.writable or prop.format != "bool":
                raise ValueError("设备不支持电源控制")
            return "on", action == "turn_on"
        prop_name = {
            "set_brightness": "brightness",
            "set_color_temperature": "color-temperature",
            "set_temperature": "target-temperature",
            "set_mode": "mode",
            "set_speed": "fan-level",
        }.get(action, "")
        prop = device.properties.get(prop_name)
        if prop is None or not prop.writable:
            raise ValueError("该动作没有可写属性")
        raw = str(value if value is not None else "").strip()
        if not raw:
            raise ValueError("该动作必须提供 value")
        if prop.value_list:
            normalized = VALUE_ALIASES.get(raw.casefold(), raw).casefold()
            for label, item in prop.value_list.items():
                if normalized in {label.casefold(), str(item).casefold()}:
                    return prop_name, item
            raise ValueError(f"value 只允许：{', '.join(prop.value_list)}")
        if prop.value_range:
            try:
                number = float(raw)
            except ValueError as exc:
                raise ValueError("value 必须是数字") from exc
            minimum, maximum = prop.value_range[:2]
            step = prop.value_range[2] if len(prop.value_range) > 2 else 1
            if not minimum <= number <= maximum:
                raise ValueError(f"value 必须在 {minimum:g} 到 {maximum:g} 之间")
            if step <= 0 or not math.isfinite(step):
                raise ValueError("设备属性步长无效")
            steps = (number - minimum) / step
            if not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-6):
                raise ValueError(f"value 必须按 {step:g} 递增")
            if prop.format.startswith(("int", "uint")):
                if not number.is_integer():
                    raise ValueError("value 必须是整数")
                number = int(number)
            return prop_name, number
        raise ValueError("该属性没有安全的可选值或范围")

    def validate_scheduled_action(self, device_key: str, action: str, value: object) -> object:
        """Validate a planned action without connecting or changing any device."""
        device = self.devices.get(device_key)
        if device is None or not device.enabled:
            raise ValueError("设备未授权或已不存在")
        if action == "get_state" or action not in device.actions():
            raise ValueError("该设备不支持此自动化动作")
        if action in {"turn_on", "turn_off"} and value is not None:
            raise ValueError("开关动作不需要 value")
        name, wanted = self._validated_value(device, action, value)
        prop = device.properties[name]
        if not prop.readable:
            raise ValueError("该属性不支持状态确认")
        self._validate_property_value(prop, wanted)
        return None if action in {"turn_on", "turn_off"} else wanted

    @staticmethod
    def _values_equal(actual: object, wanted: object) -> bool:
        if isinstance(wanted, (bool, int)):
            return actual == wanted
        if isinstance(actual, (int, float)) and isinstance(wanted, (int, float)):
            return math.isclose(float(actual), float(wanted), rel_tol=0, abs_tol=1e-6)
        return actual == wanted

    @staticmethod
    def _display_value(prop: PropertySpec, value: object) -> object:
        for label, item in prop.value_list.items():
            if item == value:
                return label
        if (
            prop.name == "on"
            and isinstance(value, (bool, int, float))
            and value in (0, 1)
        ):
            return "开启" if value else "关闭"
        return value

    async def _audit_result(
        self,
        alias: str,
        action: str,
        ok: bool,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output = result or {"ok": ok, "device": alias, "action": action}
        if error:
            output["error"] = scrub(error)[:300]
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "device": alias or "未知设备",
            "action": action or "未知动作",
            "ok": ok,
            "detail": scrub(
                output.get("message")
                or (
                    "设备已接收指令，执行结果需确认"
                    if output.get("outcome") == "accepted"
                    else "已执行并回读"
                )
            )[:160]
            if ok
            else scrub(error)[:160],
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if os.name != "nt":
            os.chmod(self.audit_path, stat.S_IRUSR | stat.S_IWUSR)
        return output

    def read_audit(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        result: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return list(reversed(result))
