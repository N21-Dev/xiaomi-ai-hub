"""Scoped Xiaomi cloud infrared controls; raw codes never leave this adapter.

Cloud controller routes follow hass-xiaomi-miot/remote.py. Chuangmi learning
RPCs follow python-miio/chuangmi_ir.py. Cloud firmware may reject learning RPCs;
an acknowledged transmission is never presented as appliance state readback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miservice import MiIOService

from .kelvinator_ir import (MODE_OPTIONS, SWING_OPTIONS, SETTING_SCHEMA,
                            decode_learned, settings_from_state, apply_settings, encode_state, protocol_template)
from .miot_tools import DeviceRecord, MiotToolManager, _secure_replace

CONTROLLERS = "/v2/irdevice/controllers"
KEYS = "/v2/irdevice/controller/keys"
CLICK = "/v2/irdevice/controller/key/click"
LEARNING_MODELS = {"chuangmi.ir.v2", "chuangmi.remote.v2", "chuangmi.remote.h102a03"}
LABELS = {
    "power": "电源",
    "on": "开机",
    "off": "关机",
    "power_on": "开机",
    "power_off": "关机",
    "volume_up": "音量 +",
    "volume_down": "音量 −",
    "vol+": "音量 +",
    "vol-": "音量 −",
    "mute": "静音",
    "up": "上",
    "down": "下",
    "left": "左",
    "right": "右",
    "ok": "确认",
    "menu": "菜单",
    "back": "返回",
    "home": "主页",
    "mode": "模式",
    "temperature_up": "温度 +",
    "temperature_down": "温度 −",
    "channel_up": "频道 +",
    "channel_down": "频道 −",
    "play": "播放",
    "pause": "暂停",
    "input": "输入源",
    "fan": "风速",
    "swing": "扫风",
}


class _CloudRejected(ValueError):
    pass


class _InputRejected(ValueError):
    pass


def _opaque(*values: object) -> str:
    return hashlib.sha256("\0".join(map(str, values)).encode()).hexdigest()[:24]


def _text(value: object, fallback: str = "") -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return fallback
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value)).strip()[:80] or fallback


def _numeric_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,12}", value):
        return int(value)
    return None


def _unwrap(value: object) -> object:
    """Unwrap cloud/RPC envelopes while refusing every explicit error."""
    for _ in range(8):
        if isinstance(value, dict):
            if value.get("error"):
                error = value["error"]
                code = (
                    _numeric_code(error.get("code"))
                    if isinstance(error, dict)
                    else None
                )
                raise _CloudRejected(
                    f"小米云端拒绝操作（{code}）" if code else "小米云端拒绝操作"
                )
            code = _numeric_code(value.get("code"))
            if code is not None and code != 0:
                raise _CloudRejected(f"小米云端拒绝操作（{code}）")
            if "result" in value:
                value = value["result"]
                continue
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]
            continue
        return value
    raise _CloudRejected("小米云端返回了无法识别的结果")


def _accepted(value: object) -> bool:
    value = _unwrap(value)
    if value == "ok" or value == ["ok"]:
        return True
    if type(value) is int and value == 0:
        return True
    if isinstance(value, dict):
        return _numeric_code(value.get("code")) == 0 or value.get("success") is True
    return False


class InfraredAdapter:
    """A manually operated adapter restricted to devices in the active account."""

    def __init__(self, home: MiotToolManager):
        self.home = home
        self.path = Path(home.settings.state_dir) / "infrared-buttons.json"
        self._catalog: dict[str, dict[str, Any]] = {}
        self._learning: dict[str, dict[str, Any]] = {}
        self._learned = self._load()
        self.ac_path = Path(home.settings.state_dir) / "infrared-ac-settings.json"
        try:
            payload = json.loads(self.ac_path.read_text(encoding="utf-8"))
            devices = payload.get("devices", {})
            self._ac_states = devices if isinstance(devices, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            self._ac_states = {}
        self.ac_devices_path = Path(home.settings.state_dir) / "infrared-ac-devices.json"
        try:
            payload = json.loads(self.ac_devices_path.read_text(encoding="utf-8"))
            devices = payload.get("devices", {})
            self._ac_devices = devices if isinstance(devices, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            self._ac_devices = {}

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            values = payload.get("devices", {})
            return values if isinstance(values, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save(self) -> None:
        _secure_replace(self.path, {"version": 1, "devices": self._learned})

    def _check(self, device: DeviceRecord, *, online: bool = False) -> None:
        if self.home.devices.get(device.key) is not device or not device.enabled:
            raise _InputRejected("设备授权或目录已变化，请刷新设备列表")
        if not self.supports(device):
            raise _InputRejected("此设备没有已验证的红外适配器")
        if online and device.cloud_online is False:
            raise _InputRejected("红外遥控器离线，请先连接设备")

    @staticmethod
    def supports(device: DeviceRecord) -> bool:
        return device.model in LEARNING_MODELS

    def _scope(self, device: DeviceRecord) -> str:
        return _opaque(self.home.settings.mi_user_id, device.did)

    async def _request(
        self, service: MiIOService, uri: str, payload: dict[str, Any]
    ) -> object:
        def prepare(token, cookies):
            cookies["PassportDeviceId"] = token["deviceId"]
            return MiIOService.sign_data(uri, payload, token["xiaomiio"][0])

        async with asyncio.timeout(10):
            response = await service.account.mi_request(
                "xiaomiio",
                service.server + uri,
                prepare,
                {"x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2"},
                relogin=False,
            )
        if not isinstance(response, dict) or _numeric_code(response.get("code")) != 0:
            _unwrap(response)
            raise _CloudRejected("小米云端没有返回成功结果")
        if "result" not in response:
            raise _CloudRejected("小米云端没有返回结果")
        # Validate nested errors before returning the result. Keep the result's
        # original list shape because a catalogue with one controller is a list.
        _unwrap(response["result"])
        return response["result"]

    async def _rpc(
        self, service: MiIOService, device: DeviceRecord, method: str, params: dict
    ) -> object:
        return await self._request(
            service,
            "/home/rpc/" + device.did,
            {
                "id": 1,
                "method": method,
                "accessKey": "IOS00026747c5acafc2",
                "params": params,
            },
        )

    async def _refresh(
        self, device: DeviceRecord, service: MiIOService
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        response = _unwrap(
            await self._request(service, CONTROLLERS, {"parent_id": device.did})
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("controllers"), list
        ):
            raise _CloudRejected("云端没有返回已配对遥控器目录")
        remotes: list[dict[str, Any]] = []
        dispatch: dict[tuple[str, str], tuple[str, int]] = {}
        partial = False
        for controller in response["controllers"][:100]:
            if time.monotonic() >= deadline:
                partial = True
                break
            if not isinstance(controller, dict):
                continue
            did = controller.get("did")
            if not isinstance(did, (str, int)) or not str(did) or isinstance(did, bool):
                continue
            # Reject a conflicting explicit parent even when the server returned
            # it inside a scoped query. Unscoped client IDs are never accepted.
            if str(controller.get("parent_id", device.did)) != device.did:
                continue
            remote_key = _opaque(device.did, "remote", did)
            remote = {
                "key": remote_key,
                "name": _text(controller.get("name"), "已配对遥控器"),
                "category": _text(
                    controller.get("category_name") or controller.get("category"),
                    "红外设备",
                ),
                "buttons": [],
            }
            try:
                self._check(device, online=True)
                keys = _unwrap(await self._request(service, KEYS, {"did": str(did)}))
                if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
                    raise _CloudRejected("云端没有返回按键目录")
                for item in keys["keys"][:300]:
                    if not isinstance(item, dict):
                        continue
                    key_id = _numeric_code(item.get("id"))
                    if key_id is None or key_id <= 0:
                        continue
                    button_key = _opaque(device.did, did, "button", key_id)
                    if (remote_key, button_key) in dispatch:
                        continue
                    label = _text(item.get("display_name") or item.get("name"), "按键")
                    remote["buttons"].append(
                        {"key": button_key, "label": LABELS.get(label.lower(), label)}
                    )
                    dispatch[remote_key, button_key] = (str(did), key_id)
            except Exception:  # noqa: BLE001 - isolate failures of remote catalogues
                remote["message"] = "按键目录暂不可用，请刷新重试"
                partial = True
            remotes.append(remote)
        self._check(device)
        catalog = {
            "remotes": remotes,
            "dispatch": dispatch,
            "fetched": time.monotonic(),
            "partial": partial,
        }
        self._catalog[self._scope(device)] = catalog
        return catalog

    def _save_ac(self) -> None:
        _secure_replace(self.ac_path, {"version": 1, "devices": self._ac_states})

    def _ac_template(self, device: DeviceRecord, key: object) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        self._check(device)
        if not isinstance(key, str):
            raise _InputRejected("请选择空调配置")
        definitions = self._ac_devices.get(self._scope(device), {})
        registered = definitions.get(key) if isinstance(definitions, dict) else None
        if isinstance(registered, dict):
            if registered.get("protocol") != "kelvinator":
                raise _InputRejected("空调协议尚未支持")
            state = protocol_template()
            item = {"label": registered.get("label", "空调"), "source_kind": "protocol"}
            fingerprint = hashlib.sha256(b"kelvinator:1:" + state).hexdigest()
        else:
            item = self._learned.get(self._scope(device), {}).get(key)
            if not isinstance(item, dict):
                raise _InputRejected("找不到此设备的空调配置")
            try:
                state = decode_learned(item.get("code"))
            except ValueError as exc:
                raise _InputRejected(str(exc)) from exc
            fingerprint = hashlib.sha256(item["code"].encode()).hexdigest()
        records = self._ac_states.get(self._scope(device), {})
        record = records.get(key, {}) if isinstance(records, dict) else {}
        if not isinstance(record, dict) or record.get("source_fingerprint") != fingerprint:
            record = {"source_fingerprint": fingerprint}
        try:
            if record.get("settings"):
                saved = record["settings"]
                if isinstance(saved, dict) and saved.get("turbo") is True and saved.get("quiet") is True:
                    # Older panels allowed contradictory flags. Restore normal
                    # fan mode without discarding unrelated settings/permission.
                    saved = {**saved, "turbo": False, "quiet": False}
                state = apply_settings(state, saved)
        except ValueError:
            record = {"source_fingerprint": fingerprint}
        settings = settings_from_state(state)
        if settings["turbo"] and settings["quiet"]:
            state = apply_settings(state, {"turbo": False, "quiet": False})
        return state, record, item

    def ac_profiles(self, device: DeviceRecord) -> list[dict[str, Any]]:
        self._check(device)
        profiles = []
        definitions = self._ac_devices.get(self._scope(device), {})
        keys = list(definitions) if isinstance(definitions, dict) else []
        keys += [key for key in self._learned.get(self._scope(device), {}) if key not in keys]
        for key in keys:
            try:
                state, record, item = self._ac_template(device, key)
            except (_InputRejected, ValueError):
                continue
            profiles.append({
                "key": key, "name": _text(item.get("label"), "空调"),
                "protocol": "kelvinator", "protocol_label": "Kelvinator / 部分格力兼容机型",
                "settings": settings_from_state(state),
                "source_kind": "protocol" if item.get("source_kind") == "protocol" else "learned_signal",
                "requires_learning": False,
                "settings_source": "last_sent" if record.get("settings") else "protocol_defaults" if item.get("source_kind") == "protocol" else "learned_signal",
                "actual_state_known": False,
                "ai_control_enabled": record.get("ai_control_enabled") is True,
                "last_attempt": record.get("last_attempt"),
                "mode_options": [{"value": value, "label": label} for value, label in MODE_OPTIONS],
                "swing_options": [{"value": value, "label": label} for value, label in SWING_OPTIONS],
                "unsupported": ["定时开关", "自定义睡眠曲线", "机型专用功能"],
            })
        return profiles

    def device_entries(self) -> list[dict[str, Any]]:
        """Local AC views, separate from the authorized physical device registry.

        Empty gateways keep a setup entry. These entries cannot be addressed as
        physical devices; commands still use the parent and a validated profile.
        """
        entries = []
        for device in self.home.devices.values():
            if not self.supports(device):
                continue
            profiles = self.ac_profiles(device) if device.enabled else []
            for profile in profiles or [None]:
                profile_key = profile["key"] if profile else ""
                name = _text(profile.get("name"), "空调") if profile else "空调"
                name = re.sub(r"^(?:打开|开启|关闭|关掉)\s*|\s*(?:开机|关机)$", "", name).strip() or "空调"
                entries.append({
                    "key": f"ir-ac:{device.key}:{profile_key or 'pending'}",
                    "name": name, "alias": name,
                    "category": "air-conditioner", "category_label": "空调",
                    "model": profile["protocol_label"] if profile else "待选择空调协议",
                    "enabled": device.enabled, "cloud_online": device.cloud_online,
                    "state": {}, "online": False, "stale": False, "observed_at": None,
                    "controls": [], "properties": [], "actions": [],
                    "actual_state_known": False,
                    "support_note": f"通过 {device.alias} 发送红外指令；空调实际状态无法回读。",
                    "infrared_ac": {"parent_key": device.key, "parent_name": device.alias,
                                    "profile_key": profile_key, "ready": profile is not None},
                    "ac_profile": profile,
                })
        return entries

    def tool_definitions(self) -> list[dict[str, Any]]:
        targets = []
        for device in self.home.devices.values():
            if not device.enabled or not self.supports(device):
                continue
            for profile in self.ac_profiles(device):
                if profile["ai_control_enabled"]:
                    targets.append({"device": device.alias, "profile_key": profile["key"], "name": profile["name"]})
        if not targets:
            return []
        return [{"type": "function", "function": {
            "name": "control_infrared_ac",
            "description": (
                "查询或控制用户在面板明确允许 AI 控制的红外空调。面板授权已经是用户确认；"
                "唯一目标的明确请求必须直接执行，不要再次询问确认或要求用户点击确认。只能使用目录里的设备和空调档案。"
                "get_settings 是协议默认设置、源信号或上次发送设置，不是空调实时状态。set_settings 仅在用户明确"
                "要求现在操作且目标唯一时使用；支持开关、模式、温度、风速和扫风等。"
                "强劲和静音互斥；开启一种会取消另一种。单独设置风速会退出两种模式；"
                "若同时指定风速和一种开启的特殊模式，该模式优先，风速作为退出后的档位。"
                "红外成功回执只代表已发送，不能声称家电已生效；未知结果不自动重发。"
                "本工具只执行即时空调设置；延期或重复任务必须由 manage_automations 创建。自动/除湿温度固定25℃。可用目录："
                + json.dumps(targets, ensure_ascii=False)
            ),
            "parameters": {"type": "object", "additionalProperties": False,
                "properties": {
                    "device": {"type": "string", "enum": sorted({target["device"] for target in targets})},
                    "profile_key": {"type": "string", "enum": sorted({target["profile_key"] for target in targets})},
                    "action": {"type": "string", "enum": ["get_settings", "set_settings"]},
                    "settings": {"type": "object", "additionalProperties": False, "properties": SETTING_SCHEMA},
                }, "required": ["device", "profile_key", "action"]},
        }}]

    def validate_ac_settings(self, device_key: str, profile_key: str, settings: object) -> dict[str, Any]:
        """Use the send codec and saved permission checks without any I/O."""
        device = self.home.devices.get(device_key)
        if device is None:
            raise ValueError("红外设备不存在")
        self._check(device)
        template, record, _ = self._ac_template(device, profile_key)
        if record.get("ai_control_enabled") is not True:
            raise ValueError("这台空调的 AI 控制权限已关闭")
        apply_settings(template, settings)
        return dict(settings)

    async def execute_tool(self, name: str, arguments: object) -> dict[str, Any]:
        try:
            if name != "control_infrared_ac":
                raise _InputRejected("不支持的红外 AI 工具")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict) or set(arguments) - {"device", "profile_key", "action", "settings"}:
                raise _InputRejected("红外 AI 参数无效")
            device = next((device for device in self.home.devices.values()
                           if device.enabled and device.alias == arguments.get("device")), None)
            if device is None:
                raise _InputRejected("红外设备未授权或不存在")
            state, record, item = self._ac_template(device, arguments.get("profile_key"))
            if record.get("ai_control_enabled") is not True:
                raise _InputRejected("请先在空调面板开启允许 AI 控制")
            if arguments.get("action") == "get_settings":
                if "settings" in arguments:
                    raise _InputRejected("查询设置不能携带控制参数")
                return {"ok": True, "actual_state_known": False,
                        "settings": settings_from_state(state), "last_attempt": record.get("last_attempt"),
                        "settings_source": "last_sent" if record.get("settings") else "protocol_defaults" if item.get("source_kind") == "protocol" else "learned_signal",
                        "message": "仅为协议默认设置、已学习信号或上次发送设置，不是空调实时状态"}
            if arguments.get("action") != "set_settings":
                raise _InputRejected("不支持的空调操作")
            # Confirmation is derived from saved per-profile user permission,
            # never from model-provided confirmation parameters.
            result = await self.execute(device, "ir_ac_send", {
                "profile_key": arguments.get("profile_key"), "settings": arguments.get("settings")
            }, confirmed=True, ai_request=True)
            return {**result, "device": device.alias, "device_key": device.key, "action": "ir_ac_send"}
        except (ValueError, TypeError, _InputRejected):
            return {"ok": False, "outcome": "failed", "message": "空调目标、参数或 AI 控制权限无效，请检查面板设置"}

    def _learning_public(self, device: DeviceRecord) -> dict[str, Any]:
        state = self._learning.get(self._scope(device), {})
        expired = state.get("expires", 0) <= time.time()
        if expired:
            self._learning.pop(self._scope(device), None)
            state = {}
        return {
            "supported": device.model in LEARNING_MODELS,
            "active": bool(state) and not bool(state.get("code")),
            "ready": bool(state.get("code")),
            "slot": state.get("slot"),
            "expires_at": datetime.fromtimestamp(state["expires"], UTC).isoformat()
            if state
            else None,
            "message": "已捕获按键，请命名保存"
            if state.get("code")
            else (
                "请把原遥控器对准设备并按键，再读取结果"
                if state
                else "可尝试学习实体遥控器按键；需要设备固件支持云端学习指令"
            ),
        }

    async def describe(
        self, device: DeviceRecord, refresh: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": "infrared",
            "available": False,
            "message": "",
            "remotes": [],
            "learned_buttons": [],
            "ac_profiles": [],
            "controls": [],
            "learning": {"supported": False, "active": False, "ready": False},
        }
        try:
            self._check(device)
            scope = self._scope(device)
            result["controls"] = [
                {
                    "action": action,
                    "label": label,
                    "available": device.cloud_online is not False or local,
                    "requires_confirmation": confirm,
                    "reason": ""
                    if device.cloud_online is not False or local
                    else "设备离线",
                }
                for action, label, confirm, local in [
                    ("ir_ac_create", "添加协议空调", False, True),
                    ("ir_ac_send", "发送空调设置", False, False),
                    ("ir_ac_ai_permission", "保存空调 AI 控制权限", False, True),
                    ("ir_send", "发送遥控按键", True, False),
                    ("ir_send_learned", "发送学习按键", True, False),
                    ("ir_learn_start", "开始学习", True, False),
                    ("ir_learn_read", "读取学习结果", False, False),
                    ("ir_learn_save", "保存学习按键", False, True),
                    ("ir_learn_rename", "重命名学习按键", False, True),
                    ("ir_learn_delete", "删除学习按键", True, True),
                ]
            ]
            result["learning"] = self._learning_public(device)
            result["learned_buttons"] = [
                {"key": key, "label": _text(item.get("label"), "学习按键")}
                for key, item in self._learned.get(scope, {}).items()
                if isinstance(item, dict)
            ]
            result["ac_profiles"] = self.ac_profiles(device)
            if device.cloud_online is False:
                result["message"] = "红外遥控器离线，已保存按键将在设备上线后可用"
                return result
            result["available"] = True
            catalog = self._catalog.get(scope)
            if (
                refresh
                or catalog is None
                or time.monotonic() - catalog["fetched"] > 300
            ):
                async with self.home._lock:
                    self._check(device, online=True)
                    service = await self.home._connect()
                    self._check(device, online=True)
                    catalog = await self._refresh(device, service)
            result["remotes"] = catalog["remotes"]
            result["message"] = (
                "部分遥控器按键目录暂不可用，请刷新重试"
                if catalog["partial"]
                else "已加载米家配对遥控器；红外发送无法确认家电实际状态"
                if catalog["remotes"]
                else "已加载空调控制面板；红外无法读取实际状态"
                if result["ac_profiles"]
                else "已加载本面板保存的学习按键；米家配对目录为空"
                if result["learned_buttons"]
                else "米家暂未返回已配对遥控器。可先在米家添加，或尝试学习实体遥控器按键"
            )
        except Exception:  # noqa: BLE001 - cloud failures must not disclose payloads
            result["message"] = "红外目录暂不可用，请检查米家授权并刷新重试"
        return result

    @staticmethod
    def _params(value: object, allowed: set[str]) -> dict[str, Any]:
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - allowed:
            raise _InputRejected("红外操作参数无效")
        return value

    @staticmethod
    def _label(value: object) -> str:
        if not isinstance(value, str) or not _text(value) or len(value.strip()) > 60:
            raise _InputRejected("按键名称应为 1 至 60 个字符")
        return _text(value)

    async def execute(
        self,
        device: DeviceRecord,
        action: str,
        value: object = None,
        confirmed: bool = False,
        ai_request: bool = False,
    ) -> dict[str, Any]:
        sent = False
        ac_attempt = None
        try:
            self._check(device)
            known = {
                "ir_ac_send", "ir_ac_ai_permission", "ir_ac_create",
                "ir_send",
                "ir_send_learned",
                "ir_learn_start",
                "ir_learn_read",
                "ir_learn_save",
                "ir_learn_delete",
                "ir_learn_rename",
            }
            if action not in known:
                raise _InputRejected("不支持此红外操作")
            if (
                action
                in {"ir_send", "ir_send_learned", "ir_learn_start", "ir_learn_delete"}
                and confirmed is not True
            ):
                return {
                    "ok": False,
                    "outcome": "confirmation_required",
                    "message": "请确认红外按键操作",
                }
            async with self.home._lock:
                self._check(device)
                scope = self._scope(device)
                buttons = self._learned.setdefault(scope, {})
                learning = self._learning_public(device)
                if action == "ir_ac_create":
                    params = self._params(value, {"protocol", "label"})
                    if params.get("protocol") != "kelvinator":
                        raise _InputRejected("仅支持已实现的 Kelvinator 空调协议")
                    label = self._label(params.get("label"))
                    definitions = self._ac_devices.setdefault(scope, {})
                    key = "protocol-" + _opaque(scope, "kelvinator", label)
                    if key not in definitions and len(definitions) >= 20:
                        raise _InputRejected("此遥控器最多配置20台协议空调")
                    before = dict(definitions)
                    definitions[key] = {"protocol": "kelvinator", "label": label}
                    try:
                        _secure_replace(self.ac_devices_path, {"version": 1, "devices": self._ac_devices})
                    except OSError:
                        self._ac_devices[scope] = before
                        raise
                    return {"ok": True, "outcome": "confirmed", "profile_key": key,
                            "message": "协议空调已添加，可直接生成并发送设置，无需学习或绑定按键"}
                if action in {"ir_ac_send", "ir_ac_ai_permission"}:
                    params = self._params(value, {"profile_key", "settings"} if action == "ir_ac_send" else {"profile_key", "enabled"})
                    key = params.get("profile_key")
                    template, record, _ = self._ac_template(device, key)
                    record = dict(record)
                    if action == "ir_ac_ai_permission":
                        if type(params.get("enabled")) is not bool:
                            raise _InputRejected("AI 控制权限必须明确选择开启或关闭")
                        record["ai_control_enabled"] = params["enabled"]
                        self._ac_states.setdefault(scope, {})[key] = record
                        self._save_ac()
                        return {"ok": True, "outcome": "confirmed", "message": "已开启这台空调的 AI 控制" if params["enabled"] else "已关闭这台空调的 AI 控制"}
                    if ai_request and record.get("ai_control_enabled") is not True:
                        raise _InputRejected("这台空调的 AI 控制权限已关闭")
                    state = apply_settings(template, params.get("settings"))
                    settings = settings_from_state(state)
                    self._check(device, online=True)
                    service = await self.home._connect()
                    self._check(device, online=True)
                    if ai_request and self._ac_template(device, key)[1].get("ai_control_enabled") is not True:
                        raise _InputRejected("这台空调的 AI 控制权限已关闭")
                    ac_attempt = {"settings": settings, "requested_at": datetime.now(UTC).isoformat(), "outcome": "pending"}
                    record["last_attempt"] = ac_attempt
                    self._ac_states.setdefault(scope, {})[key] = record
                    self._save_ac()
                    sent = True
                    result = await self._rpc(service, device, "miIO.ir_play", {"freq": 38400, "code": encode_state(state)})
                    if not _accepted(result):
                        raise _CloudRejected("设备未确认空调红外指令发送")
                    ac_attempt["outcome"] = "accepted"
                    record["settings"] = settings
                    self._save_ac()
                    return {"ok": True, "outcome": "accepted", "actual_state_known": False,
                            "settings": settings, "message": "空调指令已发送"}
                if action in {"ir_learn_save", "ir_learn_delete", "ir_learn_rename"}:
                    params = self._params(
                        value,
                        {"label"}
                        if action == "ir_learn_save"
                        else {"button_key", "label"}
                        if action == "ir_learn_rename"
                        else {"button_key"},
                    )
                    if action == "ir_learn_save":
                        if not learning["ready"]:
                            raise _InputRejected("请先学习并读取一个按键，再命名保存")
                        label = self._label(params.get("label"))
                        if len(buttons) >= 100:
                            raise _InputRejected(
                                "此设备已保存 100 个学习按键，请先删除不需要的按键"
                            )
                        captured = self._learning[scope]
                        key = _opaque(scope, secrets.token_hex(16))
                        buttons[key] = {
                            "label": label,
                            "code": captured["code"],
                            "frequency": 38400,
                        }
                    else:
                        key = params.get("button_key")
                        if not isinstance(key, str) or key not in buttons:
                            raise _InputRejected("找不到此设备的学习按键")
                        if action == "ir_learn_delete":
                            del buttons[key]
                        else:
                            buttons[key]["label"] = self._label(params.get("label"))
                    self._save()
                    if action == "ir_learn_save":
                        self._learning.pop(scope, None)
                    return {
                        "ok": True,
                        "outcome": "confirmed",
                        "message": "学习按键已删除"
                        if action == "ir_learn_delete"
                        else "学习按键已保存",
                    }

                self._check(device, online=True)
                service = await self.home._connect()
                self._check(device, online=True)
                if action == "ir_send":
                    params = self._params(value, {"remote_key", "button_key"})
                    remote_key, button_key = (
                        params.get("remote_key"),
                        params.get("button_key"),
                    )
                    if not isinstance(remote_key, str) or not isinstance(
                        button_key, str
                    ):
                        raise _InputRejected("请选择此设备的遥控器和按键")
                    catalog = self._catalog.get(scope)
                    if catalog is None or time.monotonic() - catalog["fetched"] > 300:
                        catalog = await self._refresh(device, service)
                    target = catalog["dispatch"].get((remote_key, button_key))
                    if target is None:
                        raise _InputRejected(
                            "此按键不在当前设备的米家遥控器目录中，请刷新"
                        )
                    self._check(device, online=True)
                    sent = True
                    result = await self._request(
                        service, CLICK, {"did": target[0], "key_id": target[1]}
                    )
                    # This route commonly returns {}: the validated outer code
                    # acknowledges dispatch even when it has no RPC result.
                    if result != {} and result is not None and not _accepted(result):
                        raise _CloudRejected("云端未确认红外按键发送")
                elif action == "ir_send_learned":
                    params = self._params(value, {"button_key"})
                    key = params.get("button_key")
                    if not isinstance(key, str) or key not in buttons:
                        raise _InputRejected("找不到此设备的学习按键")
                    item = buttons[key]
                    if not self._valid_code(item.get("code")):
                        raise _InputRejected("保存的红外按键无效，请重新学习")
                    self._check(device, online=True)
                    sent = True
                    result = await self._rpc(
                        service,
                        device,
                        "miIO.ir_play",
                        {"freq": 38400, "code": item["code"]},
                    )
                    if not _accepted(result):
                        raise _CloudRejected("设备未确认红外按键发送")
                elif action == "ir_learn_start":
                    params = self._params(value, {"slot"})
                    slot = params.get("slot", 999999)
                    if type(slot) is not int or not 1 <= slot <= 1000000:
                        raise _InputRejected("学习槽位应为 1 至 1000000 的整数")
                    self._learning.pop(scope, None)
                    self._check(device, online=True)
                    sent = True
                    result = await self._rpc(
                        service, device, "miIO.ir_learn", {"key": str(slot)}
                    )
                    if not _accepted(result):
                        raise _CloudRejected(
                            "设备未确认进入学习模式，固件可能不支持云端学习"
                        )
                    self._learning[scope] = {"slot": slot, "expires": time.time() + 60}
                    return {
                        "ok": True,
                        "outcome": "accepted",
                        "message": "设备已接收学习指令，请对准设备按原遥控器按键，然后读取结果",
                    }
                else:  # ir_learn_read never writes to the physical device.
                    self._params(value, set())
                    if not learning["active"] and not learning["ready"]:
                        raise _InputRejected("请先开始学习；学习窗口为 60 秒")
                    capture = self._learning[scope]
                    result = _unwrap(
                        await self._rpc(
                            service,
                            device,
                            "miIO.ir_read",
                            {"key": str(capture["slot"])},
                        )
                    )
                    self._check(device)
                    if not isinstance(result, dict) or not self._valid_code(
                        result.get("code")
                    ):
                        raise _InputRejected(
                            "尚未捕获按键，请按下实体遥控器按键后重新读取"
                        )
                    if str(result.get("key", capture["slot"])) != str(capture["slot"]):
                        raise _InputRejected("学习结果槽位不匹配，请重新学习")
                    capture["code"] = result["code"]
                    capture["expires"] = time.time() + 300
                    return {
                        "ok": True,
                        "outcome": "confirmed",
                        "message": "按键已捕获，请在 5 分钟内命名保存",
                    }
                return {
                    "ok": True,
                    "outcome": "accepted",
                    "message": "已发送红外按键；请在家电上确认，红外控制没有状态回读",
                }
        except _CloudRejected as exc:
            self._finish_ac_attempt(ac_attempt, "failed")
            return {"ok": False, "outcome": "failed", "message": str(exc)}
        except (ValueError, _InputRejected) as exc:
            self._finish_ac_attempt(ac_attempt, "failed")
            return {"ok": False, "outcome": "failed", "message": str(exc)}
        except Exception:  # noqa: BLE001 - cloud failures must not disclose payloads
            self._finish_ac_attempt(ac_attempt, "unconfirmed" if sent else "failed")
            return {
                "ok": False,
                "outcome": "unconfirmed" if sent else "failed",
                "message": "指令结果未知，请先检查设备，勿连续重复发送"
                if sent
                else "红外服务暂不可用，请检查米家连接后重试",
            }

    def _finish_ac_attempt(self, attempt: dict[str, Any] | None, outcome: str) -> None:
        if attempt is not None:
            attempt["outcome"] = outcome
            try:
                self._save_ac()
            except OSError:
                pass  # A pending attempt on disk remains explicitly uncertain.

    @staticmethod
    def _valid_code(value: object) -> bool:
        return (
            isinstance(value, str)
            and 16 <= len(value) <= 32768
            and bool(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value))
        )
