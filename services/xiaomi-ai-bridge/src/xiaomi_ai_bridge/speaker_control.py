"""Fixed XiaoAI cloud controls, independent of the selected AI speaker.

MiNA responses contain two success codes.  A successful HTTP request (or a
truthy response) alone does not establish that a speaker accepted a command.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from miservice import MiNAService

from .miot_tools import DeviceRecord
from .xiaomi import XiaomiClient


class _ControlError(ValueError):
    """An intentionally public, credential-free diagnostic."""


class _CloudResponseError(_ControlError):
    pass


class _CloudRejected(_CloudResponseError):
    pass


def _zero(value: Any) -> bool:
    return not isinstance(value, bool) and value in (0, "0")


def _unpack_response(payload: Any, *, require_info: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict) or "code" not in payload:
        raise _CloudResponseError("小米云返回了无法验证的响应")
    if not _zero(payload["code"]):
        raise _CloudRejected("小米云拒绝了请求，请检查连接状态")
    data = payload.get("data")
    if not isinstance(data, dict) or "code" not in data:
        raise _CloudResponseError("小米云没有返回设备确认码")
    if not _zero(data["code"]):
        raise _CloudRejected("音箱拒绝了请求，可能离线或不支持此功能")
    info = data.get("info")
    if info is None or info == "":
        if require_info:
            raise _CloudResponseError("音箱没有返回播放状态")
        return {}
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except (TypeError, ValueError) as exc:
            raise _CloudResponseError("音箱返回的状态格式无法解析") from exc
    if not isinstance(info, dict):
        raise _CloudResponseError("音箱返回的状态格式无法解析")
    if ("code" in info and not _zero(info["code"])) or info.get("success") is False:
        raise _CloudRejected("音箱未接受此操作")
    if info.get("error") not in (None, "", 0, False, {}):
        raise _CloudRejected("音箱报告操作错误")
    return info


class _NoReplayAccount:
    """Keep the library's hardware adaptation without replaying a write."""

    def __init__(self, account: Any, guard: Callable[[], None]) -> None:
        self.account = account
        self.guard = guard
        self.sent = False

    async def mi_request(
        self, sid: str, url: str, data: Any, headers: dict[str, str]
    ) -> Any:
        self.guard()
        token = getattr(self.account, "token", None)
        service_token = token.get("micoapi") if isinstance(token, dict) else None
        if (
            not isinstance(service_token, (tuple, list))
            or len(service_token) < 2
            or not isinstance(service_token[1], str)
            or not service_token[1]
        ):
            raise _ControlError("小爱会话已失效，请先重新连接")
        self.sent = True
        return await self.account.mi_request(sid, url, data, headers, relogin=False)


class SpeakerController:
    MAP_TTL = 300.0
    STATE_TTL = 20.0
    READ_TIMEOUT = 8.0
    WRITE_TIMEOUT = 10.0
    CONFIRM_ACTIONS = frozenset({"speaker_tts", "speaker_url"})
    SIMPLE_ACTIONS: ClassVar[dict[str, str]] = {
        "speaker_play": "player_play",
        "speaker_pause": "player_pause",
        "speaker_stop": "player_stop",
    }

    def __init__(
        self,
        xiaomi: XiaomiClient,
        *,
        device_is_current: Callable[[DeviceRecord], bool] | None = None,
    ):
        self.xiaomi = xiaomi
        self.device_is_current = device_is_current
        self._mina: Any = None
        self._devices: dict[str, dict[str, str]] = {}
        self._mapped_at = 0.0
        self._map_lock = asyncio.Lock()
        self._states: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def supports(device: DeviceRecord) -> bool:
        return device.category == "speaker" or ".wifispeaker." in device.model

    # 音箱专用动作：不走 MIoT 能力目录，由小爱云接口执行。
    # AI 工具枚举里要把它们列出来，否则模型会认为「只能查状态」。
    ACTIONS = (
        "speaker_play",
        "speaker_pause",
        "speaker_stop",
        "speaker_volume",
        "speaker_loop",
        "speaker_tts",
        "speaker_url",
    )
    ACTION_LABELS = {
        "speaker_play": "继续播放",
        "speaker_pause": "暂停",
        "speaker_stop": "停止",
        "speaker_volume": "音量",
        "speaker_loop": "循环模式",
        "speaker_tts": "文字播报",
        "speaker_url": "播放音频地址",
    }

    @staticmethod
    def coerce(action: str, value: Any) -> Any:
        """AI 工具的参数都是字符串，数字型动作先转成 int 再交给 validate。"""
        if action in {"speaker_volume", "speaker_loop"} and isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return value

    @classmethod
    def validate(cls, action: str, value: Any = None) -> Any:
        """Validate before the bridge interrupts an AI response or takes a lock."""
        if not isinstance(action, str):
            raise _ControlError("不支持的小爱控制操作")
        if action in cls.SIMPLE_ACTIONS:
            if value is not None:
                raise _ControlError("播放按钮不接受额外参数")
            return None
        if action == "speaker_volume":
            if type(value) is not int or not 0 <= value <= 100:
                raise _ControlError("音量必须是 0 到 100 的整数")
            return value
        if action == "speaker_loop":
            if type(value) is not int or value not in (0, 1):
                raise _ControlError("循环模式仅支持单曲循环和列表循环")
            return value
        if action == "speaker_tts":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
                raise _ControlError("播报文本需要 1 到 500 个字符")
            text = value.strip()
            if any(ord(char) < 32 and char not in "\n\t" for char in text):
                raise _ControlError("播报文本包含无效控制字符")
            return text
        if action == "speaker_url":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048:
                raise _ControlError("音频地址长度不正确")
            text = value.strip()
            try:
                parts = urlsplit(text)
                valid = (
                    parts.scheme in {"http", "https"}
                    and bool(parts.hostname)
                    and parts.username is None
                    and parts.password is None
                    and parts.port != 0
                    and not parts.fragment
                    and not any(char.isspace() or ord(char) < 32 for char in text)
                    and "\\" not in text
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise _ControlError("请使用不含账号密码的 HTTP 或 HTTPS 音频地址")
            return text
        raise _ControlError("不支持的小爱控制操作；其他动作请使用设备完整能力面板")

    def _guard(self, device: DeviceRecord, mina: Any) -> None:
        if self.xiaomi.device is None:
            raise _ControlError("小爱连接尚未完成，请稍后刷新")
        if self.device_is_current is not None and not self.device_is_current(device):
            raise _ControlError("设备列表已经更新，请刷新后重试")
        if not self.supports(device):
            raise _ControlError("此设备不是小爱音箱")
        if not device.enabled:
            raise _ControlError("此设备已禁用，请先启用设备")
        if device.cloud_online is False:
            raise _ControlError("米家显示音箱离线，请先检查设备网络")
        if mina is None or self.xiaomi.mina is not mina:
            raise _ControlError("小爱会话未连接或已变更，请刷新后重试")

    async def _mapped_device(
        self, device: DeviceRecord, mina: Any, *, refresh: bool = False
    ) -> dict[str, str]:
        async with self._map_lock:
            changed = self._mina is not mina
            if changed or refresh or time.monotonic() - self._mapped_at >= self.MAP_TTL:
                self._guard(device, mina)
                rows = await asyncio.wait_for(mina.device_list(), self.READ_TIMEOUT)
                self._guard(device, mina)
                if not isinstance(rows, list):
                    raise _ControlError("小爱云没有返回有效的音箱列表")
                mapped: dict[str, dict[str, str]] = {}
                ambiguous: set[str] = set()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    did = str(row.get("miotDID") or row.get("did") or "")
                    device_id = str(row.get("deviceID") or "")
                    if not did or not device_id:
                        continue
                    if did in mapped and mapped[did]["device_id"] != device_id:
                        ambiguous.add(did)
                    mapped[did] = {
                        "device_id": device_id,
                        "hardware": str(row.get("hardware") or "").upper(),
                    }
                for did in ambiguous:
                    mapped.pop(did, None)
                if changed:
                    self._states.clear()
                self._devices = mapped
                self._mina = mina
                self._mapped_at = time.monotonic()
            mapped_device = self._devices.get(device.did)
            if not mapped_device:
                raise _ControlError("此设备尚未匹配到小爱云音箱，请刷新设备列表")
            return mapped_device

    @staticmethod
    def _public_state(info: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for name, limit in (("volume", 100), ("loop_type", 3), ("status", 2)):
            raw = info.get(name)
            if isinstance(raw, str) and raw.isdigit():
                raw = int(raw)
            if type(raw) is int and 0 <= raw <= limit:
                state[{"loop_type": "loop", "status": "status"}.get(name, name)] = raw
        state["playback"] = {0: "idle", 1: "playing", 2: "paused"}.get(
            state.pop("status", None), "unknown"
        )
        detail = info.get("play_song_detail")
        if not isinstance(detail, dict):
            detail = {}
        for name, keys in (
            ("title", ("title", "name")),
            ("artist", ("artist", "artistName")),
            ("album", ("album", "albumName")),
        ):
            for key in keys:
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    state[name] = value.strip()[:200]
                    break
        state["updated_at"] = datetime.now(UTC).isoformat()
        return state

    async def _read_state(
        self, device: DeviceRecord, mina: Any, mapped: dict[str, str]
    ) -> dict[str, Any]:
        self._guard(device, mina)
        response = await asyncio.wait_for(
            mina.player_get_status(mapped["device_id"]), self.READ_TIMEOUT
        )
        self._guard(device, mina)
        info = _unpack_response(response, require_info=True)
        state = self._public_state(info)
        if (
            not any(key in state for key in ("volume", "loop", "title"))
            and state["playback"] == "unknown"
        ):
            raise _CloudResponseError("音箱没有返回可读取的播放数据")
        self._states[device.did] = (time.monotonic(), state)
        return dict(state)

    @staticmethod
    def _controls(available: bool, reason: str = "") -> list[dict[str, Any]]:
        controls: list[dict[str, Any]] = [
            {"action": "speaker_play", "label": "继续播放", "type": "button"},
            {"action": "speaker_pause", "label": "暂停", "type": "button"},
            {"action": "speaker_stop", "label": "停止", "type": "button"},
            {
                "action": "speaker_volume",
                "label": "音量",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "unit": "%",
            },
            {
                "action": "speaker_loop",
                "label": "循环模式",
                "type": "select",
                "options": [
                    {"value": 0, "label": "单曲循环"},
                    {"value": 1, "label": "列表循环"},
                ],
                "hint": "循环模式是否生效取决于音箱固件与当前播放来源",
            },
            {
                "action": "speaker_tts",
                "label": "文字播报",
                "type": "text",
                "max_length": 500,
                "requires_confirmation": True,
            },
            {
                "action": "speaker_url",
                "label": "播放音频地址",
                "type": "url",
                "max_length": 2048,
                "requires_confirmation": True,
                "hint": "音箱必须能访问这个 HTTP / HTTPS 音频地址",
            },
        ]
        for control in controls:
            control.update(available=available, reason=reason)
            control.setdefault("requires_confirmation", False)
        for action, label in (
            ("speaker_previous", "上一首"),
            ("speaker_next", "下一首"),
        ):
            controls.append(
                {
                    "action": action,
                    "label": label,
                    "type": "button",
                    "available": False,
                    "reason": "请使用完整能力中的上一首 / 下一首动作，当前云播放接口未确认此操作",
                    "requires_confirmation": False,
                }
            )
        return controls

    async def describe(
        self, device: DeviceRecord, refresh: bool = False
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "kind": "speaker",
            "available": False,
            "message": "",
            "state": {},
        }
        mina = self.xiaomi.mina
        try:
            self._guard(device, mina)
            mapped = await self._mapped_device(device, mina, refresh=refresh)
            self._guard(device, mina)
        except Exception as exc:  # noqa: BLE001 - never expose credentials in errors
            output["message"] = (
                str(exc)
                if isinstance(exc, _ControlError)
                else "小爱云连接暂不可用，请稍后刷新"
            )
            output["controls"] = self._controls(False, output["message"])
            return output
        output.update(available=True, controls=self._controls(True))
        cached = self._states.get(device.did)
        if not refresh and cached and time.monotonic() - cached[0] < self.STATE_TTL:
            output["state"] = deepcopy(cached[1])
            output["message"] = "已连接小爱云"
            return output
        try:
            output["state"] = await self._read_state(device, mina, mapped)
            output["message"] = "已连接小爱云"
        except Exception:  # noqa: BLE001 - a status failure need not disable controls
            output["message"] = "已匹配音箱，播放状态暂不可读；操作结果以设备回包为准"
            if cached:
                output["state"] = {**cached[1], "stale": True}
            # A device disabled or session changed during the read cannot be used.
            try:
                self._guard(device, mina)
            except ValueError as exc:
                output.update(available=False, message=str(exc))
                output["controls"] = self._controls(False, str(exc))
        return output

    async def execute(
        self,
        device: DeviceRecord,
        action: str,
        value: Any = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        writer_account: _NoReplayAccount | None = None
        try:
            value = self.validate(action, value)
            if action in self.CONFIRM_ACTIONS and confirmed is not True:
                raise _ControlError("请先确认将在这台音箱上播放声音")
            mina = self.xiaomi.mina
            self._guard(device, mina)
            mapped = await self._mapped_device(device, mina)
            self._guard(device, mina)
            writer_account = _NoReplayAccount(
                mina.account, lambda: self._guard(device, mina)
            )
            writer = MiNAService(writer_account)
            # The pinned MiService fork uses this cache to choose the proper API
            # for LX05 and other play_music-only models without a second lookup.
            writer.device2hardware[mapped["device_id"]] = mapped["hardware"]
            device_id = mapped["device_id"]
            if action in self.SIMPLE_ACTIONS:
                command = getattr(writer, self.SIMPLE_ACTIONS[action])(device_id)
            elif action == "speaker_volume":
                command = writer.player_set_volume(device_id, value)
            elif action == "speaker_loop":
                command = writer.player_set_loop(device_id, value)
            elif action == "speaker_tts":
                command = writer.text_to_speech(device_id, value)
            else:
                command = writer.play_by_url(device_id, value)
            response = await asyncio.wait_for(command, self.WRITE_TIMEOUT)
            self._states.pop(device.did, None)
            _unpack_response(response)
        except Exception as exc:  # noqa: BLE001 - report uncertainty without replay
            sent = bool(writer_account and writer_account.sent)
            if sent:
                self._states.pop(device.did, None)
            rejected = isinstance(exc, _CloudRejected)
            outcome = "failed" if not sent or rejected else "unconfirmed"
            if isinstance(exc, _ControlError):
                message = str(exc)
            else:
                message = (
                    "请求已经发出，但未取得可靠确认；请刷新状态后再决定是否重试"
                    if sent
                    else "小爱云连接暂不可用，请刷新后重试"
                )
            return {"ok": False, "outcome": outcome, "message": message}

        result: dict[str, Any] = {
            "ok": True,
            "outcome": "accepted",
            "message": "音箱已接收指令；实际播放结果以音箱为准",
        }
        if action in self.CONFIRM_ACTIONS:
            return result
        expected = {
            "speaker_volume": ("volume", value),
            "speaker_loop": ("loop", value),
            "speaker_play": ("playback", "playing"),
            "speaker_pause": ("playback", "paused"),
            "speaker_stop": ("playback", "idle"),
        }[action]
        try:
            state = await self._read_state(device, mina, mapped)
        except Exception:  # noqa: BLE001 - do not resend an acknowledged operation
            return result
        result["readback"] = state
        key, target = expected
        if key in state and state[key] != "unknown":
            if state[key] == target:
                result.update(outcome="confirmed", message="音箱状态已回读确认")
            else:
                result.update(
                    ok=False,
                    outcome="unconfirmed",
                    message="音箱已接收指令，但回读状态尚未达到设置值，请稍后刷新",
                )
        return result
