"""Allowlisted, read-only local MiWiFi telemetry for a configured RA67 router."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import math
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .field_catalog import build_field_catalog
from .miot_tools import DeviceRecord

SOURCE = "xiaomi.router.local"
PUBLIC_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
READ_ENDPOINTS = frozenset(
    {
        "xqsystem/init_info",
        "misystem/status",
        "xqnetwork/wan_info",
        "xqnetwork/mode",
        "misystem/devicelist",
        "misystem/topo_graph",
    }
)
MODES = {0: "路由模式", 1: "无线中继", 2: "有线中继（AP）", 9: "Mesh 节点"}
DEFINITIONS = {
    "cpu-percent": ("CPU 使用率", "%", "measurement", "misystem/status.cpu.load"),
    "memory-percent": ("内存使用率", "%", "measurement", "misystem/status.mem.usage"),
    "uptime-seconds": ("连续运行时长", "秒", "counter", "misystem/status.upTime"),
    "system-temperature": (
        "系统温度",
        "℃",
        "measurement",
        "misystem/status.temperature",
    ),
    "connected-device-count": (
        "本机连接终端",
        "台",
        "measurement",
        "misystem/status.count.online",
    ),
    "known-device-count": (
        "本机记录终端总数",
        "台",
        "counter",
        "misystem/status.count.all",
    ),
    "wan-download-bytes-per-second": (
        "WAN 下载速度",
        "B/s",
        "measurement",
        "misystem/status.wan.downspeed",
    ),
    "wan-upload-bytes-per-second": (
        "WAN 上传速度",
        "B/s",
        "measurement",
        "misystem/status.wan.upspeed",
    ),
    "wan-download-bytes": (
        "WAN 累计下载",
        "B",
        "counter",
        "misystem/status.wan.download",
    ),
    "wan-upload-bytes": ("WAN 累计上传", "B", "counter", "misystem/status.wan.upload"),
    "wan-link": ("WAN 连接状态", "", "state", "xqnetwork/wan_info.info.link"),
    "wan-uptime-seconds": (
        "WAN 连接时长",
        "秒",
        "counter",
        "xqnetwork/wan_info.info.uptime",
    ),
    "router-mode": ("工作模式", "", "state", "xqnetwork/mode.mode"),
    "firmware-version": ("固件版本", "", "metadata", "xqsystem/init_info.romversion"),
    "hardware-model": ("硬件型号", "", "metadata", "xqsystem/init_info.hardware"),
    "memory-total-mb": ("总内存", "MB", "metadata", "misystem/status.mem.total"),
    "cpu-cores": ("CPU 核心数", "核", "metadata", "misystem/status.cpu.core"),
    "cpu-frequency-mhz": ("CPU 频率", "MHz", "metadata", "misystem/status.cpu.hz"),
    "memory-frequency-mhz": ("内存频率", "MHz", "metadata", "misystem/status.mem.hz"),
    "memory-type": ("内存类型", "", "metadata", "misystem/status.mem.type"),
    "rom-channel": ("固件通道", "", "metadata", "misystem/status.hardware.channel"),
}


class _RouterError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _number(value: Any, *, maximum: float = 1e18) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not 0 <= number <= maximum:
        return None
    return int(number) if number.is_integer() else number


def _unit_number(value: Any, unit: str) -> int | float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*" + re.escape(unit) + r"\s*", value, re.IGNORECASE
    )
    return _number(match[1]) if match else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value.strip()


class RouterSource:
    METADATA_TTL = 6 * 3600

    def __init__(self, settings: Any):
        self.base_url = str(getattr(settings, "router_url", "") or "").rstrip("/")
        self.device_key = str(getattr(settings, "router_device_key", "") or "")
        self.credentials_file = getattr(settings, "router_credentials_file", None)
        self.poll_seconds = getattr(settings, "router_poll_seconds", 60)
        if type(self.poll_seconds) is not int or not 30 <= self.poll_seconds <= 3600:
            raise ValueError("路由器采样间隔应为 30 至 3600 秒")
        if self.base_url:
            try:
                parsed = urlsplit(self.base_url)
                address = ipaddress.ip_address(parsed.hostname or "")
                valid = (
                    parsed.scheme in {"http", "https"}
                    and not (
                        parsed.username
                        or parsed.password
                        or parsed.query
                        or parsed.fragment
                        or parsed.path
                    )
                    and parsed.port != 0
                    and address.is_private
                    and not (
                        address.is_loopback
                        or address.is_unspecified
                        or address.is_multicast
                    )
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("路由器地址必须是 NAS 可访问的局域网 HTTP/HTTPS 地址")
        self._session: aiohttp.ClientSession | None = None
        self._stok: str | None = None
        self._identity: dict[str, Any] = {}
        self._identity_at = 0.0
        self._metadata_at = 0.0
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._inflight: asyncio.Task | None = None
        self._login_retry_at = 0.0
        self._credential_revision: tuple[int, int] | None = None

    async def close(self) -> None:
        if self._inflight and not self._inflight.done():
            self._inflight.cancel()
            await asyncio.gather(self._inflight, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._stok = None

    def _guard(self, device: DeviceRecord) -> None:
        if not self.base_url or not self.device_key or not self.credentials_file:
            raise _RouterError("尚未配置路由器本地只读连接")
        if (
            device.key != self.device_key
            or device.model != "xiaomi.router.ra67"
            or not device.enabled
        ):
            raise _RouterError("路由器设备与本地授权配置不匹配或已停用")

    async def _request(
        self,
        endpoint: str,
        *,
        authenticated: bool = True,
        login_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        is_login = endpoint == "xqsystem/login" and login_data is not None
        if endpoint not in READ_ENDPOINTS and not is_login:
            raise _RouterError("此路由器接口不在只读白名单中")
        if login_data is not None and not is_login:
            raise _RouterError("仅路由器登录允许发送表单")
        if authenticated and not self._stok:
            raise _RouterError("路由器管理会话不可用", auth=True)
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        prefix = f"/;stok={self._stok}" if authenticated else ""
        url = f"{self.base_url}/cgi-bin/luci{prefix}/api/{endpoint}"
        try:
            async with self._session.request(
                "POST" if is_login else "GET",
                url,
                data=login_data,
                allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    raise _RouterError("路由器管理会话已过期，请稍后刷新", auth=True)
                if response.status != 200:
                    raise _RouterError("路由器没有返回有效数据，请检查本地连接")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise _RouterError("路由器响应超过读取限制")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise _RouterError("路由器返回的数据格式无法识别") from None
                if (
                    not isinstance(payload, dict)
                    or type(payload.get("code")) is not int
                    or payload["code"] != 0
                ):
                    code = payload.get("code") if isinstance(payload, dict) else None
                    raise _RouterError(
                        "路由器未接受读取请求",
                        auth=type(code) is int and code in {401, 403},
                    )
                return payload
        except (aiohttp.ClientError, TimeoutError):
            raise _RouterError("路由器本地连接超时或不可达") from None

    async def _verify(self, device: DeviceRecord) -> None:
        self._guard(device)
        info = await self._request("xqsystem/init_info", authenticated=False)
        self._guard(device)
        if (
            info.get("model") != device.model
            or str(info.get("hardware", "")).upper() != "RA67"
        ):
            self._identity = {}
            self._identity_at = 0
            self._stok = None
            raise _RouterError("本地地址的型号与账号路由器不匹配，未发送登录凭据")
        self._identity = {"hardware-model": "RA67"}
        if version := _text(info.get("romversion")):
            self._identity["firmware-version"] = version
        self._identity_at = time.monotonic()

    async def _login(self, device: DeviceRecord) -> None:
        self._guard(device)
        try:
            path = Path(self.credentials_file)
            stat = path.stat()
            revision = (stat.st_mtime_ns, stat.st_size)
            if stat.st_size > 8192:
                raise ValueError()
            credential = json.loads(path.read_text(encoding="utf-8"))
            password = credential.get("password")
            if not isinstance(password, str) or not password or len(password) > 256:
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise _RouterError("路由器管理员凭据文件不可用，请检查 NAS 配置") from None
        if revision != self._credential_revision:
            self._login_retry_at = 0
            self._credential_revision = revision
        if time.monotonic() < self._login_retry_at:
            raise _RouterError("路由器登录暂未完成，请稍后再试")
        # Reverify identity immediately before sending any password-derived data.
        await self._verify(device)
        self._guard(device)
        mac = ":".join(
            f"{uuid.getnode():012x}"[index : index + 2] for index in range(0, 12, 2)
        )
        nonce = f"0_{mac}_{int(time.time())}_{secrets.randbelow(1000)}"
        inner = hashlib.sha1((password + PUBLIC_KEY).encode()).hexdigest()
        hashed = hashlib.sha1((nonce + inner).encode()).hexdigest()
        self._login_retry_at = time.monotonic() + 60
        result = await self._request(
            "xqsystem/login",
            authenticated=False,
            login_data={
                "username": "admin",
                "logtype": "2",
                "nonce": nonce,
                "password": hashed,
            },
        )
        self._guard(device)
        token = result.get("token")
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,256}", token
        ):
            raise _RouterError("路由器登录未返回有效会话，请核对管理员凭据")
        self._stok = token
        self._login_retry_at = 0

    async def snapshot(
        self, device: DeviceRecord, refresh: bool = False
    ) -> dict[str, Any]:
        try:
            self._guard(device)
        except _RouterError as exc:
            return self._empty(str(exc))
        if (
            not refresh
            and self._cache
            and time.monotonic() - self._cache_at < self.poll_seconds
        ):
            return copy.deepcopy(self._cache)
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._fetch(device, refresh))
        return copy.deepcopy(await asyncio.shield(self._inflight))

    def _empty(self, message: str) -> dict[str, Any]:
        return {
            "kind": "router",
            "source": SOURCE,
            "available": False,
            "identity_verified": False,
            "message": message,
            "state": {},
            "fields": [],
            "observed_at": None,
            "stale": False,
        }

    async def _fetch(self, device: DeviceRecord, refresh: bool) -> dict[str, Any]:
        try:
            async with asyncio.timeout(25):
                if not self._stok:
                    await self._login(device)
                elif (
                    refresh or time.monotonic() - self._identity_at >= self.METADATA_TTL
                ):
                    await self._verify(device)
                self._guard(device)
                endpoints = ("misystem/status", "xqnetwork/wan_info", "xqnetwork/mode")
                replies = await asyncio.gather(
                    *(self._request(endpoint) for endpoint in endpoints),
                    return_exceptions=True,
                )
                self._guard(device)
                received = dict(zip(endpoints, replies, strict=True))
                for reply in replies:
                    if isinstance(reply, _RouterError) and reply.auth:
                        self._stok = None
                status = received["misystem/status"]
                if isinstance(status, dict):
                    hardware = status.get("hardware")
                    if isinstance(hardware, dict) and hardware.get("platform") not in (
                        None,
                        "RA67",
                    ):
                        self._stok = None
                        self._identity = {}
                        raise _RouterError("路由器状态中的硬件身份不匹配")
                    count = status.get("count")
                    if (
                        not isinstance(count, dict)
                        or _number(count.get("online")) is None
                    ):
                        try:
                            received["misystem/devicelist"] = await self._request(
                                "misystem/devicelist"
                            )
                        except _RouterError as exc:
                            received["misystem/devicelist"] = exc
                self._guard(device)
                result = self._project(received, refresh)
        except Exception as exc:  # noqa: BLE001 - no raw auth response may escape
            result = copy.deepcopy(self._cache) if self._cache else self._empty("")
            result.update(
                available=False,
                identity_verified=bool(self._identity),
                stale=bool(result.get("observed_at")),
                message=str(exc)
                if isinstance(exc, _RouterError)
                else "路由器读取未完成，请稍后刷新",
            )
            for field in result.get("fields", []):
                if field.get("status") == "ok":
                    field.update(status="stale", message="本地连接未更新，显示上次读数")
        result["checked_at"] = _now()
        self._cache, self._cache_at = result, time.monotonic()
        return result

    def _project(self, replies: dict[str, Any], refresh: bool) -> dict[str, Any]:
        now = _now()
        status = replies.get("misystem/status")
        status = status if isinstance(status, dict) else {}
        mode_reply = replies.get("xqnetwork/mode")
        mode = mode_reply.get("mode") if isinstance(mode_reply, dict) else None
        wan_reply = replies.get("xqnetwork/wan_info")
        info = wan_reply.get("info") if isinstance(wan_reply, dict) else None
        ap_mode = type(mode) is int and mode in {1, 2, 9}
        wan_unavailable = info is False
        wan_uncertain = ap_mode and not isinstance(info, dict)
        values: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        sources: dict[str, str] = {}
        for key, value in self._identity.items():
            values[key] = value
        for parent, key, target in (
            ("cpu", "load", "cpu-percent"),
            ("mem", "usage", "memory-percent"),
        ):
            section = status.get(parent)
            raw = (
                _number(section.get(key), maximum=1)
                if isinstance(section, dict)
                else None
            )
            if raw is not None:
                values[target] = round(raw * 100, 2)
        if (uptime := _number(status.get("upTime"))) is not None:
            values["uptime-seconds"] = uptime
        temperature = _number(status.get("temperature"), maximum=200)
        if temperature is not None and temperature > 0:
            values["system-temperature"] = temperature
        elif temperature == 0:
            unavailable["system-temperature"] = "此固件使用 0 表示未提供温度传感器读数"
        count = status.get("count")
        if isinstance(count, dict):
            for source, target in (
                ("online", "connected-device-count"),
                ("all", "known-device-count"),
            ):
                if (value := _number(count.get(source))) is not None:
                    values[target] = value
        client_reply = replies.get("misystem/devicelist")
        clients = client_reply.get("list") if isinstance(client_reply, dict) else None
        if "connected-device-count" not in values and isinstance(clients, list):
            valid_clients = [
                row
                for row in clients
                if isinstance(row, dict) and row.get("online") in (0, 1, "0", "1")
            ]
            if len(valid_clients) == len(clients):
                values["connected-device-count"] = sum(
                    str(row["online"]) == "1" for row in valid_clients
                )
                sources["connected-device-count"] = "misystem/devicelist.list.online"
        if type(mode) is int and mode in MODES:
            values["router-mode"] = mode
        wan = status.get("wan")
        wan_keys = {key for key in DEFINITIONS if key.startswith("wan-")}
        if wan_unavailable:
            for key in wan_keys:
                unavailable[key] = (
                    "有线/无线中继或 Mesh 节点未启用独立 WAN，此指标不适用"
                    if ap_mode
                    else "本机接口未提供独立 WAN 状态，此指标暂不适用"
                )
        elif not wan_uncertain:
            if isinstance(wan, dict):
                for source, target in (
                    ("downspeed", "wan-download-bytes-per-second"),
                    ("upspeed", "wan-upload-bytes-per-second"),
                    ("download", "wan-download-bytes"),
                    ("upload", "wan-upload-bytes"),
                ):
                    if (value := _number(wan.get(source))) is not None:
                        values[target] = value
            if isinstance(info, dict):
                if type(info.get("link")) is int and info["link"] in (0, 1):
                    values["wan-link"] = bool(info["link"])
                if (value := _number(info.get("uptime"))) is not None:
                    values["wan-uptime-seconds"] = value
                    if "wan-link" not in values:
                        values["wan-link"] = value > 0
                        sources["wan-link"] = "xqnetwork/wan_info.info.uptime"
        cpu = status.get("cpu") if isinstance(status.get("cpu"), dict) else {}
        mem = status.get("mem") if isinstance(status.get("mem"), dict) else {}
        hardware = (
            status.get("hardware") if isinstance(status.get("hardware"), dict) else {}
        )
        for target, raw, unit in (
            ("memory-total-mb", mem.get("total"), "MB"),
            ("cpu-frequency-mhz", cpu.get("hz"), "MHz"),
            ("memory-frequency-mhz", mem.get("hz"), "MHz"),
        ):
            if (value := _unit_number(raw, unit)) is not None:
                values[target] = value
        if (value := _number(cpu.get("core"), maximum=256)) is not None:
            values["cpu-cores"] = value
        for target, raw in (
            ("memory-type", mem.get("type")),
            ("rom-channel", hardware.get("channel")),
        ):
            if value := _text(raw):
                values[target] = value
        previous_fields = {
            field["key"]: field for field in (self._cache or {}).get("fields", [])
        }
        previous_state = (self._cache or {}).get("state", {})
        state, declarations = {}, []
        metadata_due = (
            not self._cache
            or refresh
            or time.monotonic() - self._metadata_at >= self.METADATA_TTL
        )
        fresh_dynamic = False
        for name, (label, unit, role, source_field) in DEFINITIONS.items():
            key = "local." + name
            field = {
                "key": key,
                "label": label,
                "unit": unit,
                "role": role,
                "type": "boolean"
                if name == "wan-link"
                else "enum"
                if name == "router-mode"
                else "string"
                if name
                in {"firmware-version", "hardware-model", "memory-type", "rom-channel"}
                else "number",
                "source": SOURCE,
                "source_field": sources.get(name, source_field),
                "readable": True,
                "writable": False,
                "attempted_at": now,
            }
            if role in {"measurement", "counter", "state"}:
                field.update(priority="primary", refresh_seconds=self.poll_seconds)
            if name == "router-mode":
                field["options"] = [
                    {"value": value, "label": label} for value, label in MODES.items()
                ]
            if name in unavailable:
                field.update(status="unsupported", message=unavailable[name])
            elif name in values:
                state[key] = values[name]
                unchanged_metadata = (
                    role == "metadata"
                    and not metadata_due
                    and previous_state.get(key) == values[name]
                )
                observed = (
                    previous_fields.get(key, {}).get("observed_at")
                    if unchanged_metadata
                    else now
                )
                field.update(
                    status="ok",
                    message="通过路由器本地接口读取",
                    observed_at=observed or now,
                )
                fresh_dynamic |= role != "metadata"
            elif key in previous_state:
                state[key] = previous_state[key]
                field.update(
                    status="stale",
                    message="此项本次未读到，显示上次读数",
                    observed_at=previous_fields.get(key, {}).get("observed_at"),
                )
            else:
                field.update(
                    status="unavailable", message="路由器当前没有提供此项有效读数"
                )
            declarations.append(field)
        if metadata_due:
            self._metadata_at = time.monotonic()
        partial = any(isinstance(reply, Exception) for reply in replies.values())
        return {
            "kind": "router",
            "source": SOURCE,
            "identity_verified": True,
            "available": fresh_dynamic,
            "stale": not fresh_dynamic and bool((self._cache or {}).get("observed_at")),
            "message": "已连接路由器本地只读接口"
            if fresh_dynamic and not partial
            else "部分本地指标读取失败，已保留其他成功读数"
            if fresh_dynamic
            else "路由器动态指标暂时不可读，保留上次数据",
            "state": state,
            "fields": build_field_catalog(state, declarations, source=SOURCE),
            "observed_at": now
            if fresh_dynamic
            else (self._cache or {}).get("observed_at"),
        }
