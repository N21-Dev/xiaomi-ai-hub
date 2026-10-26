from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)

SERVICE_LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin"
LONG_POLLING_URL = "https://account.xiaomi.com/longPolling/loginUrl"
LOGIN_USER_AGENT = "offici5l/migate"


def parse_xiaomi_payload(text: str) -> dict[str, Any]:
    prefix = "&&&START&&&"
    if not text.startswith(prefix):
        raise RuntimeError("小米登录返回了未知格式")
    payload = json.loads(text[len(prefix) :])
    if not isinstance(payload, dict):
        raise TypeError("小米登录返回内容无效")
    return payload


class XiaomiWebLogin:
    def __init__(self, on_credentials: Callable[[dict[str, object]], object]):
        self.on_credentials = on_credentials
        self.session: aiohttp.ClientSession | None = None
        self.task: asyncio.Task[None] | None = None
        self.state = "idle"
        self.login_url = ""
        self.message = ""
        self.expires_at = 0.0
        self.sid = "micoapi"
        self._lock = asyncio.Lock()

    def public_status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "login_url": self.login_url if self.state == "waiting" else "",
            "message": self.message,
            "expires_at": self.expires_at or None,
            "sid": self.sid,
        }

    async def start(self, sid: str = "micoapi") -> dict[str, object]:
        if sid not in {"micoapi", "xiaomiio"}:
            raise ValueError("不支持的小米服务授权范围")
        async with self._lock:
            await self._cancel_current()
            self.state = "starting"
            self.login_url = ""
            self.message = ""
            self.sid = sid
            headers = {
                "User-Agent": LOGIN_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json;charset=UTF-8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            self.session = aiohttp.ClientSession(
                headers=headers,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=45),
            )
            try:
                auth_data: dict[str, Any] = {"sid": sid, "_json": "true"}
                async with self.session.get(
                    SERVICE_LOGIN_URL, params=auth_data
                ) as response:
                    response.raise_for_status()
                    phase = parse_xiaomi_payload(await response.text())
                for key in ("serviceParam", "qs", "callback", "_sign"):
                    value = phase.get(key)
                    if value is None:
                        raise RuntimeError(f"小米登录缺少字段 {key}")
                    auth_data[key] = value
                auth_data["_json"] = "false"
                async with self.session.get(
                    LONG_POLLING_URL, params=auth_data
                ) as response:
                    response.raise_for_status()
                    polling = parse_xiaomi_payload(await response.text())
                self.login_url = str(polling.get("loginUrl") or "")
                polling_url = str(polling.get("lp") or "")
                timeout = max(30.0, min(float(polling.get("timeout") or 300), 600.0))
                if not self.login_url.startswith(
                    "https://"
                ) or not polling_url.startswith("https://"):
                    raise RuntimeError("小米登录链接无效")
                self.state = "waiting"
                self.message = (
                    "等待米家设备授权" if sid == "xiaomiio" else "等待小米账号登录"
                )
                self.expires_at = time.time() + timeout
                self.task = asyncio.create_task(self._poll(polling_url, timeout))
                return self.public_status()
            except Exception:
                await self._cancel_current()
                self.state = "error"
                raise

    async def _poll(self, polling_url: str, timeout: float) -> None:
        assert self.session is not None
        try:
            async with self.session.get(
                polling_url, timeout=aiohttp.ClientTimeout(total=timeout + 10)
            ) as response:
                response.raise_for_status()
                payload = parse_xiaomi_payload(await response.text())
            cookies = {cookie.key: cookie.value for cookie in self.session.cookie_jar}
            for key in ("deviceId", "passToken", "userId"):
                if payload.get(key):
                    cookies[key] = str(payload[key])
            missing = [
                key
                for key in ("deviceId", "passToken", "userId")
                if not cookies.get(key)
            ]
            if missing:
                raise RuntimeError("小米登录未返回完整会话")
            self.on_credentials(
                {
                    "sid": self.sid,
                    "user_id": cookies["userId"],
                    "pass_token": cookies["passToken"],
                    "account_device_id": cookies["deviceId"],
                    "c_user_id": cookies.get("cUserId", ""),
                }
            )
            self.state = "success"
            self.message = (
                "米家设备授权已保存，可以发现设备"
                if self.sid == "xiaomiio"
                else "小米账号会话已保存，正在连接音箱"
            )
            self.login_url = ""
            LOGGER.info("Xiaomi browser login completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - background login boundary
            self.state = "error"
            self.message = str(exc)
            self.login_url = ""
            LOGGER.warning("Xiaomi browser login failed: %s", exc)
        finally:
            if self.session is not None:
                await self.session.close()
                self.session = None

    async def _cancel_current(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def close(self) -> None:
        async with self._lock:
            await self._cancel_current()
