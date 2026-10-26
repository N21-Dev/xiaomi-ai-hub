"""小米健康网页登录：让用户在小米官方页面登录，自动捕获并验证 Cookie。

设计边界：
- 登录窗口始终指向 account.xiaomi.com，本服务不收集账号密码；
- 使用健康服务自己的 sid=miothealth，不能复用小爱音箱的 micoapi 会话；
- 浏览器登录返回的 userId/passToken 先交给回调做健康接口验证，成功后才持久化；
- 对外状态永远不包含 Cookie、token、userId 或小米响应原文。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..cloud.crypto import parse_login_payload
from ..cloud.endpoints import ACCOUNT_HOST, LOGIN_PATH, LOGIN_SID
from ..logging_setup import scrub

logger = logging.getLogger(__name__)

LONG_POLLING_PATH = "/longPolling/loginUrl"
LOGIN_USER_AGENT = "offici5l/migate"
_ALLOWED_HOST = "account.xiaomi.com"


class _LoginHttpClient(Protocol):
    cookies: Any

    def get(self, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


CredentialHandler = Callable[[dict[str, str]], None]
ClientFactory = Callable[[], _LoginHttpClient]


class XiaomiHealthWebLogin:
    """线程安全的网页登录状态机。

    `start()` 只完成「申请小米登录地址」，不会阻塞等待用户操作。长轮询运行在 daemon
    线程中；登录成功后由 `on_credentials` 验证健康接口并原子保存凭据。
    """

    def __init__(
        self,
        on_credentials: CredentialHandler,
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.on_credentials = on_credentials
        self.client_factory = client_factory or self._default_client
        self.clock = clock
        self._lock = threading.RLock()
        self._generation = 0
        self._client: _LoginHttpClient | None = None
        self._thread: threading.Thread | None = None
        self.state = "idle"
        self.login_url = ""
        self.qr_url = ""
        self.qr_tips = ""
        self.message = "尚未开始登录"
        self.expires_at = 0.0
        self.credentials_updated = False

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            headers={
                "User-Agent": LOGIN_USER_AGENT,
                "Accept": "application/json;charset=UTF-8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=httpx.Timeout(45.0, read=620.0),
            follow_redirects=False,
        )

    @staticmethod
    def _require_xiaomi_https(value: str, label: str) -> str:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == _ALLOWED_HOST or hostname.endswith("." + _ALLOWED_HOST)
        ):
            raise RuntimeError(f"小米{label}地址无效")
        return value

    def public_status(self) -> dict[str, object]:
        with self._lock:
            # 长轮询线程极端情况下没及时醒来，也不能让过期链接继续展示。
            if self.state == "waiting" and self.expires_at and self.clock() > self.expires_at:
                self.state = "expired"
                self.login_url = ""
                self.qr_url = ""
                self.qr_tips = ""
                self.message = "登录二维码已过期，请刷新二维码"
            return {
                "state": self.state,
                "login_url": self.login_url if self.state == "waiting" else "",
                "qr_url": self.qr_url if self.state == "waiting" else "",
                "qr_tips": self.qr_tips if self.state == "waiting" else "",
                "message": self.message,
                "expires_at": self.expires_at or None,
                "credentials_updated": self.credentials_updated,
            }

    def start(self) -> dict[str, object]:
        """申请网页登录地址并启动后台长轮询。"""
        self.cancel(message="正在重新开始登录")
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.state = "starting"
            self.login_url = ""
            self.qr_url = ""
            self.qr_tips = ""
            self.message = "正在向小米申请登录二维码"
            self.expires_at = 0.0
            self.credentials_updated = False

        client = self.client_factory()
        try:
            phase = self._initial_login(client)
            auth_data: dict[str, str] = {"sid": LOGIN_SID, "_json": "true"}
            for key in ("serviceParam", "qs", "callback", "_sign"):
                value = phase.get(key)
                if value is None:
                    raise RuntimeError(f"小米登录缺少字段 {key}")
                auth_data[key] = str(value)
            auth_data["_json"] = "false"

            polling_response = client.get(
                f"{ACCOUNT_HOST}{LONG_POLLING_PATH}", params=auth_data
            )
            polling_response.raise_for_status()
            polling = parse_login_payload(polling_response.text)
            login_url = self._require_xiaomi_https(
                str(polling.get("loginUrl") or ""), "登录"
            )
            qr_url = self._require_xiaomi_https(
                str(polling.get("qr") or ""), "二维码"
            )
            qr_tips = str(polling.get("qrTips") or "请用小米账号扫码登录")[:200]
            polling_url = self._require_xiaomi_https(
                str(polling.get("lp") or ""), "轮询"
            )
            timeout = max(30.0, min(float(polling.get("timeout") or 300), 600.0))
        except Exception:
            client.close()
            with self._lock:
                if generation == self._generation:
                    self.state = "error"
                    self.login_url = ""
                    self.qr_url = ""
                    self.qr_tips = ""
                    self.message = "无法获取小米登录二维码，请稍后重试"
            raise

        with self._lock:
            if generation != self._generation:
                client.close()
                return self.public_status()
            self._client = client
            self.state = "waiting"
            self.login_url = login_url
            self.qr_url = qr_url
            self.qr_tips = qr_tips
            self.message = "请用小米账号扫描二维码并确认登录"
            self.expires_at = self.clock() + timeout
            thread = threading.Thread(
                target=self._poll,
                args=(generation, client, polling_url, timeout),
                name="xiaomi-health-web-login",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self.public_status()

    def _initial_login(self, client: _LoginHttpClient) -> dict[str, Any]:
        response = client.get(
            f"{ACCOUNT_HOST}{LOGIN_PATH}",
            params={"sid": LOGIN_SID, "_json": "true"},
        )
        response.raise_for_status()
        payload = parse_login_payload(response.text)
        # 未登录时 miothealth 会返回 code=70016 / 登录验证失败，但仍给出网页登录所需
        # 的 _sign/qs/callback/serviceParam。这是初始化流程的正常形态；字段完整即可继续。
        required = ("serviceParam", "qs", "callback", "_sign")
        if all(payload.get(key) is not None for key in required):
            return payload
        code = payload.get("code")
        description = scrub(str(payload.get("description") or "登录初始化失败"))
        raise RuntimeError(f"小米登录初始化失败（code={code}）：{description}")

    def _poll(
        self,
        generation: int,
        client: _LoginHttpClient,
        polling_url: str,
        timeout: float,
    ) -> None:
        try:
            response = client.get(polling_url, timeout=timeout + 10)
            response.raise_for_status()
            payload = parse_login_payload(response.text)
            code = payload.get("code")
            if code not in (0, "0", None):
                description = scrub(str(payload.get("description") or "网页登录失败"))
                raise RuntimeError(f"小米网页登录失败（code={code}）：{description}")

            cookies = self._cookie_dict(client)
            for key in ("deviceId", "passToken", "userId", "cUserId"):
                value = payload.get(key)
                if value:
                    cookies[key] = str(value)
            missing = [key for key in ("deviceId", "passToken", "userId") if not cookies.get(key)]
            if missing:
                raise RuntimeError("小米登录未返回完整的健康账号会话")

            with self._lock:
                if generation != self._generation:
                    return
                self.state = "validating"
                self.login_url = ""
                self.qr_url = ""
                self.qr_tips = ""
                self.message = "正在验证小米健康接口"

            # 回调负责用 sid=miothealth 真正登录健康接口，并在验证成功后落盘。
            self.on_credentials(
                {
                    "user_id": cookies["userId"],
                    "pass_token": cookies["passToken"],
                    "device_id": cookies["deviceId"],
                    "c_user_id": cookies.get("cUserId", ""),
                }
            )
            with self._lock:
                if generation != self._generation:
                    return
                self.state = "success"
                self.message = "小米健康账号登录成功，凭据已安全保存"
                self.credentials_updated = True
                self.login_url = ""
                self.qr_url = ""
                self.qr_tips = ""
                logger.info("小米健康网页登录完成，凭据已通过 miothealth 验证")
        except Exception as exc:  # noqa: BLE001 - 外部登录边界，错误转成公开状态
            with self._lock:
                if generation != self._generation:
                    return
                self.state = "error"
                self.message = scrub(str(exc)) or type(exc).__name__
                self.login_url = ""
                self.qr_url = ""
                self.qr_tips = ""
                logger.warning("小米健康网页登录失败：%s", scrub(str(exc)))
        finally:
            client.close()
            with self._lock:
                if generation == self._generation:
                    self._client = None
                    self._thread = None

    @staticmethod
    def _cookie_dict(client: _LoginHttpClient) -> dict[str, str]:
        """读取 CookieJar，允许同名 Cookie 同时存在于多个小米子域。

        `httpx.Cookies.items()` 遇到这种合法情况会抛 CookieConflict（实测 cUserId
        同时存在于 account.xiaomi.com 与其子域）。直接遍历底层 CookieJar 可保留值，
        随后的长轮询 payload 仍会优先覆盖同名字段。
        """
        cookies = getattr(client, "cookies", None)
        jar = getattr(cookies, "jar", None)
        if jar is not None:
            result: dict[str, str] = {}
            try:
                for cookie in jar:
                    domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
                    if domain and not (
                        domain == _ALLOWED_HOST or domain.endswith("." + _ALLOWED_HOST)
                    ):
                        continue
                    name = str(getattr(cookie, "name", "") or "")
                    value = str(getattr(cookie, "value", "") or "")
                    if name and value:
                        result[name] = value
                return result
            except (AttributeError, TypeError):
                return {}
        try:
            return {str(k): str(v) for k, v in cookies.items()}
        except (AttributeError, TypeError):
            return {}

    def cancel(self, *, message: str = "登录已取消") -> dict[str, object]:
        """取消当前登录。关闭客户端可中断正在阻塞的长轮询。"""
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._thread = None
            self.state = "idle"
            self.login_url = ""
            self.qr_url = ""
            self.qr_tips = ""
            self.message = message
            self.expires_at = 0.0
            self.credentials_updated = False
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 取消动作必须幂等
                pass
        return self.public_status()

    def close(self) -> None:
        self.cancel(message="服务正在停止")
